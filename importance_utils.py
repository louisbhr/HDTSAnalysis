# importance_utils.py
"""
Zentrale Stelle fuer Feature-Importance-Normalisierung und Score-Berechnung.

Hintergrund:
Bisher wurde die Feature-Importance an mehreren Stellen unterschiedlich skaliert
(in baseline_manager.py und in jump_analyzer.py jeweils auf Summe = len(var_names)/2 = 4.5).
Dadurch war der Score um ca. Faktor 4.5 zu hoch, obwohl die Deltas plausibel waren.

Dieses Modul ist ab jetzt die EINZIGE Stelle, an der Importances normalisiert werden.
Regel: Die Summe aller genutzten Importances ist immer exakt 1.0.

Dadurch wird der Score zu einem gewichteten Mittel der absoluten Abweichungen
(in Standardabweichungen / MAD), also gut interpretierbar:
    abs_score = Summe( |delta_i| * importance_i )    mit  Summe(importance_i) = 1.0
    => abs_score ~ "durchschnittliche Abweichung in Standardabweichungen".
"""

import math

import numpy as np

# Zielsumme aller Importances. Bewusst zentral als Konstante, damit es nur EINE Wahrheit gibt.
TARGET_IMPORTANCE_SUM = 1.0

# Schwellwerte fuer die verbale Abstufung der Korrektur (auf der 1.0-Skala).
# Da abs_score ein gewichtetes Mittel der |z|-Abweichungen ist, sind das direkt
# "durchschnittliche Standardabweichungen". Empirisch (Validierungsstudie) lag
# abs_score im Bereich 1.29-2.63, die alten Schwellen 2.5/4.5 feuerten praktisch
# nie. Neu kalibriert auf ~P75/P97 der erwarteten Verteilung bei normaler Tagesform.
STEP_THRESHOLD_STRONG = 1.9   # darueber: "sehr deutlich"
STEP_THRESHOLD_MEDIUM = 1.4   # darueber: "deutlich"

# MAD -> Sigma-Schaetzer. Rohe MAD ist auf einer anderen Skala als eine Standard-
# abweichung; multipliziert mit 1.4826 wird sie (unter Normalverteilungsannahme)
# ein konsistenter Schaetzer fuer Sigma. Ohne diesen Faktor liegen individuelle
# z-Werte (MAD-basiert) um ca. 1.48x ueber den Gold-z-Werten (Std-basiert) - die
# beiden Referenz-Modi leben sonst auf verschiedenen Skalen.
MAD_CONSISTENCY = 1.4826

# Totband fuer den Trend-Score: innerhalb dieser Bandbreite gilt das Timing als
# stabil, es wird keine Richtung ausgegeben. Fixwert = Rueckfallebene und Warmstart,
# solange zu wenig eigene trend-Werte fuer das Quantil vorliegen (s.u.).
DEADBAND_TREND = 0.5

# --- Quantil-Totband (Uebergabe 7.1) --------------------------------------
# Ein FESTER Wert 0.5 wirkt je Athlet voellig verschieden: Feedback-Rate 33 % beim
# Live-Test (1 Erwachsener), aber nur 12 % auf der Validierungsgruppe. Die
# Kalibrierkurve dort: 0.25 -> 34 %, 0.30 -> 30 %, 0.40 -> 20 %, 0.50 -> 12 %.
# Ein festes Totband kalibriert also immer nur EINEN Koerper richtig.
#
# Loesung: Totband = Quantil der EIGENEN juengsten |trend|-Werte. Bei P70 liegen
# per Konstruktion ~30 % der Kontakte darueber -> die Feedback-Rate ist nicht mehr
# vom Athleten abhaengig, sondern eingestellt. Das Kind wird gegen sein eigenes
# gestriges Streuungsmass bewertet, nicht gegen eine fremde Zahl.
DEADBAND_MODE = "quantil"     # "quantil" | "fix" - Rueckfall auf DEADBAND_TREND
DEADBAND_QUANTILE = 0.70      # P70 -> Ziel-Feedback-Rate ~30 %
DEADBAND_WINDOW = 20          # Uebergabe 7.1 nennt 12-20; 20 gibt ein ruhigeres Quantil
DEADBAND_MIN_N = 8            # darunter ist ein P70 Rauschen -> Fixwert
# Leitplanken. NICHT aus den Daten abgeleitet, sondern bewusst gesetzt: ein Quantil
# kann bei sehr gleichfoermigem Springen gegen 0 kollabieren (dann wuerde jedes
# Zittern zur Richtungsansage) oder bei chaotischem Springen weglaufen (dann waere
# die Ampel stumm). Untergrenze = bester Punkt der Kalibrierkurve oben.
DEADBAND_FLOOR = 0.25
DEADBAND_CEIL = 1.20

# Konsistenz-Gate in Phase "halten": eine Richtung wird nur ausgegeben, wenn der
# Trend-Score einen ausreichend grossen Anteil des Absolut-Scores erklaert
# (sonst heben sich Einzelabweichungen gegenseitig auf -> "uneinheitlich").
CONSISTENCY_GATE = 0.6

# Phasengrenze: h_rel = h_previous / h_max. Ab hier gilt der Sprung als Phase
# "halten" (nahe Bestleistung), sonst "aufbau". NUR noch fuer den Offline-Profiler
# und Rueckwaertskompatibilitaet - die Live-Phase kommt aus der HG-Dynamik (s.u.).
PHASE_HALTEN_H_REL = 0.9

# --- diffI-Totband (Phase "aufbau") ---------------------------------------
# diffI = Integral(i) - Integral(i-1) ist am Kontaktende latenzfrei verfuegbar und
# praediziert den erst beim naechsten Kontakt messbaren Hoehengewinn HG. GRUEN im
# Aufbau erst ab diffI > DIFFI_DEADBAND (kalibriert: robuste SD von diffI bei
# stabilen Spruengen ~21.8, Median ~ -4.8) - verhindert Muenzwurf-Verhalten bei
# stabilem Springen. EINE Wahrheit fuer jump_analyzer UND esp_client.
DIFFI_DEADBAND = 22.0

# --- Live-Phasenerkennung aus der HG-Dynamik (mit Hysterese) --------------
# Einstieg "aufbau", wenn der letzte HG gross war ODER das Mittel der letzten drei
# HG deutlich positiv ist; Ausstieg erst nach mehreren "ruhigen" Spruengen
# (asymmetrische Hysterese -> kein Flattern, funktioniert auf jeder Hoehe).
HG_AUFBAU_ENTRY = 0.15    # Sofort-Einstieg Aufbau, wenn HG[i-1] > diesem Wert
HG_AUFBAU_MEAN3 = 0.08    # oder Mittel der letzten 3 HG darueber
HYSTERESE_EXIT = 2        # so viele ruhige Spruenge, bis zurueck auf "halten"

# --- Rolling-Referenz + MAD-Floor -----------------------------------------
# Referenz aus den letzten ROLL_N phasengleichen Kontakten der laufenden Session
# (ab MIN_ROLL Kontakten; davor Warmstart aus der gespeicherten Baseline). Loest
# den Gold-Fallback-Schiefstand im Halten und ist tagesformrobust.
ROLL_N = 12
MIN_ROLL = 6
# MAD-Floor: deviation = max(MAD, MAD_FLOOR_FACTOR * GoldStd). Kappt die z-Explosion
# aus kleinen, homogenen Teilmengen (individuelle MADs koennen absurd eng werden).
MAD_FLOOR_FACTOR = 0.30


def effective_deadband(trend_history):
    """Wirksames Trend-Totband aus der eigenen juengsten |trend|-Verteilung.

    trend_history: iterierbar mit den trend-Werten der letzten phasengleichen
    Kontakte, die gegen die EIGENE Referenz gescort wurden. Vorzeichen egal, es
    wird mit Betraegen gerechnet.

    Rueckgabe: Totband als float.

    WICHTIG - nur eigene Werte einspeisen. Gegen den Goldstandard liegt trend
    konstant bei +1.2 bis +2.6 Sigma (Uebergabe 5.1.3); solche Werte im Fenster
    wuerden das Quantil massiv aufblasen und die Ampel danach stumm schalten.
    Die Auswahl passiert beim Befuellen (jump_analyzer), nicht hier.

    Unter DEADBAND_MIN_N Werten oder bei DEADBAND_MODE != "quantil" gilt der
    Fixwert DEADBAND_TREND. Das Ergebnis wird auf [DEADBAND_FLOOR, DEADBAND_CEIL]
    begrenzt.
    """
    if DEADBAND_MODE != "quantil":
        return DEADBAND_TREND

    values = []
    for t in (trend_history or ()):
        try:
            t = float(t)
        except (TypeError, ValueError):
            continue
        if math.isfinite(t):
            values.append(abs(t))

    if len(values) < DEADBAND_MIN_N:
        return DEADBAND_TREND

    # Nur die juengsten DEADBAND_WINDOW Werte - Tagesform statt Sessiongedaechtnis.
    values = values[-DEADBAND_WINDOW:]
    values.sort()

    # Lineare Interpolation zwischen den Rangplaetzen (wie numpy's Default), damit
    # das Quantil bei kleinen n nicht auf einen einzelnen Wert einrastet.
    pos = DEADBAND_QUANTILE * (len(values) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        q = values[lo]
    else:
        q = values[lo] + (values[hi] - values[lo]) * (pos - lo)

    return max(DEADBAND_FLOOR, min(DEADBAND_CEIL, q))


def stepword(level):
    """Verbale Stufe aus dem Ampel-Level (1..3): 1 etwas, 2 deutlich, 3 sehr deutlich."""
    if level >= 3:
        return "sehr deutlich"
    if level >= 2:
        return "deutlich"
    return "etwas"


def determine_phase(h_previous, h_max, h_rel_threshold=PHASE_HALTEN_H_REL):
    """Bestimmt die aktuelle Phase ("aufbau" oder "halten") aus der Flughoehe
    VOR dem aktuellen Kontakt (h_previous) relativ zur Max-Hoehe (h_max).

    Ohne verwertbare Information (h_previous unbekannt oder h_max <= 0) wird
    konservativ "aufbau" angenommen (keine Timing-Bewertung, nur Output-Feedback).
    """
    if h_previous is None or h_max is None or h_max <= 0 or not np.isfinite(h_previous):
        return "aufbau"
    h_rel = h_previous / h_max
    return "halten" if h_rel >= h_rel_threshold else "aufbau"


def _clean_values(values):
    """Ersetzt NaN/Inf durch 0.0 und macht negative Werte (z.B. Korrelationen) positiv.

    Importances sind Gewichte und duerfen nie negativ, NaN oder unendlich sein.
    """
    arr = np.asarray(values, dtype=float)
    arr = np.where(np.isfinite(arr), arr, 0.0)
    arr = np.abs(arr)
    return arr


def normalize_importance(values, feature_names=None, target_sum=TARGET_IMPORTANCE_SUM):
    """Normalisiert beliebige Roh-Importances robust auf eine feste Summe (Default 1.0).

    Akzeptiert ein dict {feature: wert} ODER ein array-aehnliches Objekt.
    Rueckgabetyp entspricht dem Eingabetyp (dict -> dict, sonst np.ndarray).

    Robustheit:
      * NaN / Inf werden zu 0.0
      * negative Werte werden ueber den Betrag verwendet
      * Summe 0 (alle Gewichte ungueltig) -> Gleichverteilung statt Division durch 0
    """
    is_dict = isinstance(values, dict)

    if is_dict:
        if feature_names is None:
            feature_names = list(values.keys())
        raw = _clean_values([values.get(name, 0.0) for name in feature_names])
    else:
        raw = _clean_values(values)
        if feature_names is None:
            feature_names = [f"f{i}" for i in range(len(raw))]

    total = float(np.sum(raw))
    n = len(raw)

    if not np.isfinite(total) or total <= 0.0 or n == 0:
        # Fallback: Gleichverteilung, damit nie durch 0 geteilt wird.
        normed = np.full(n, target_sum / n if n > 0 else 0.0)
    else:
        normed = raw * (target_sum / total)

    if is_dict:
        return {name: float(w) for name, w in zip(feature_names, normed)}
    return normed


def step_label(abs_score):
    """Verbale Abstufung der Korrektur anhand des absoluten Scores (1.0-Skala)."""
    if abs_score > STEP_THRESHOLD_STRONG:
        return "sehr deutlich"
    if abs_score > STEP_THRESHOLD_MEDIUM:
        return "deutlich"
    return "etwas"


def compute_jump_score(current_features, reference, deviation, importance,
    direction, feature_order):
    """Berechnet Trend- und Absolut-Score fuer einen einzelnen Sprung.

    Parameter (alle als dict ueber feature_order indizierbar):
        current_features : aktueller Messwert je Feature
        reference        : Referenzwert (Median / GoldMean) je Feature
        deviation        : Streuung (MAD / GoldStd) je Feature
        importance       : Roh-Importance je Feature (wird hier intern auf 1.0 normiert)
        direction        : +1 / -1 Korrekturrichtung je Feature
        feature_order    : Liste der zu bewertenden Features (Reihenfolge)

    Rueckgabe: dict mit
        trend_score   : Summe( delta_i * importance_i * direction_i )   (mit Vorzeichen)
        abs_score     : Summe( |delta_i| * importance_i )               (ohne Vorzeichen)
        max_abs_delta : groesste absolute Einzelabweichung (in Std/MAD), z-Trigger
        details       : Liste von dicts pro Feature (fuer Debug-Logs)

    Es wird garantiert: keine Division durch 0, kein NaN/Inf im Ergebnis.
    """
    # Nur Features verwenden, die in allen Quellen vorhanden sind.
    used = [f for f in feature_order if f in current_features]

    # Importances der genutzten Features einsammeln und zentral auf 1.0 normieren.
    raw_imp = {f: importance.get(f, 0.0) for f in used}
    norm_imp = normalize_importance(raw_imp, feature_names=used, target_sum=TARGET_IMPORTANCE_SUM)

    details = []
    trend_score = 0.0
    abs_score = 0.0
    max_abs_delta = 0.0

    for f in used:
        cur = current_features.get(f, np.nan)
        ref = reference.get(f, np.nan)
        dev = deviation.get(f, np.nan)
        dir_mult = float(direction.get(f, 1))
        imp = float(norm_imp.get(f, 0.0))

        # Delta als z-Wert (Abweichung in Standardabweichungen / MAD).
        # Schutz vor Division durch 0 und ungueltigen Werten.
        if (not np.isfinite(cur)) or (not np.isfinite(ref)) or (not np.isfinite(dev)) or dev <= 0.0:
            delta = 0.0
        else:
            delta = (cur - ref) / dev
            if not np.isfinite(delta):
                delta = 0.0

        weighted = abs(delta) * imp        # Beitrag zum Absolut-Score
        trend_contrib = delta * imp * dir_mult

        trend_score += trend_contrib
        abs_score += weighted
        max_abs_delta = max(max_abs_delta, abs(delta))

        details.append({
            "Feature": f,
            "Wert": cur,
            "Referenz": ref,
            "Streuung": dev,
            "Delta_z": delta,
            "Importance_norm": imp,
            "Gewichteter_Anteil": weighted,
        })

    # Endgueltige Saeuberung der Aggregate.
    trend_score = float(trend_score) if np.isfinite(trend_score) else 0.0
    abs_score = float(abs_score) if np.isfinite(abs_score) else 0.0
    max_abs_delta = float(max_abs_delta) if np.isfinite(max_abs_delta) else 0.0

    return {
        "trend_score": trend_score,
        "abs_score": abs_score,
        "max_abs_delta": max_abs_delta,
        "details": details,
    }


def format_debug_table(details, max_rows=20):
    """Formatiert die Detail-Liste aus compute_jump_score als lesbare Debug-Tabelle.

    Spalten: Feature | Wert | Referenz | Streuung | Delta | Importance | gew.Anteil
    """
    header = (f"{'Feature':<12}{'Wert':>14}{'Referenz':>14}"
    f"{'Streuung':>12}{'Delta':>9}{'Imp':>8}{'gew.Anteil':>12}")
    lines = [header, "-" * len(header)]
    for d in details[:max_rows]:
        lines.append(
            f"{d['Feature']:<12}"
            f"{d['Wert']:>14.4g}"
            f"{d['Referenz']:>14.4g}"
            f"{d['Streuung']:>12.4g}"
            f"{d['Delta_z']:>9.3f}"
            f"{d['Importance_norm']:>8.3f}"
            f"{d['Gewichteter_Anteil']:>12.4f}"
        )
    return "\n".join(lines)
