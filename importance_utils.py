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

import numpy as np

# Zielsumme aller Importances. Bewusst zentral als Konstante, damit es nur EINE Wahrheit gibt.
TARGET_IMPORTANCE_SUM = 1.0

# Schwellwerte fuer die verbale Abstufung der Korrektur (auf der 1.0-Skala).
# Da abs_score ein gewichtetes Mittel der |z|-Abweichungen ist, sind das direkt
# "durchschnittliche Standardabweichungen". Hier zentral kalibrierbar.
STEP_THRESHOLD_STRONG = 4.5   # darueber: "sehr deutlich"
STEP_THRESHOLD_MEDIUM = 2.5   # darueber: "deutlich"


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
