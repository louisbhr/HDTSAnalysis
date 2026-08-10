#!/usr/bin/env python3
"""
tests/test_refactor.py

Einfaches Abnahme-Skript fuer den HDTS-Validierungs-Refactor (kein pytest noetig).

Ausfuehren:
    python tests/test_refactor.py

Exit-Code 0 = alle Tests gruen, 1 = mindestens ein Test fehlgeschlagen.

Getestet wird:
  a) Profiler-Durchlauf mit synthetischem Kraftsignal: Contact_t/Integral/diffI
     vorhanden, diffI[i] == Integral[i] - Integral[i-1].
  b) compute_jump_score mit den 6 Score-Features: Summe der genutzten Importances
     == 1.0, trend_score == abs_score wenn alle Deltas dieselbe Richtung haben.
  c) Eine alte Baseline-CSV ohne "Mode"-Spalte wird rueckwaertskompatibel geladen
     (jump_analyzer.load_profile UND profiler.load_scoring_profile).
  d) Phasen-Weiche: h_rel 0.5 -> "aufbau", h_rel 0.95 -> "halten".
  e) Ampel-Logik (esp_client.classify_ampel): alle 8 Zweige (4 je Phase) plus
     Aufbau-Fallback ohne individuelle Aufbau-Baseline.
  f) Aufbau-Fallback in der Pipeline: baseline_manager speichert bei zu wenigen
     Aufbau-Spruengen KEINE Aufbau-Zeilen (kein Goldstandard-Fallback fuer
     "aufbau"); der Analyzer erkennt das und sperrt die Richtungslichter.
"""
import os
import sys
import shutil
import tempfile
import traceback

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import importance_utils
import profiler
import jump_analyzer as jump_analyzer_module


def _make_synthetic_signal(n_jumps=6, fs=1000, contact_samples=250, flight_samples=400,
                            peak_force=8000.0):
    """Baut ein synthetisches Kraftsignal aus Sinus-Halbwellen als "Spruenge",
    getrennt durch (fast) kraftfreie Flugphasen."""
    rng = np.random.default_rng(42)
    signal = []
    for _ in range(n_jumps):
        t = np.linspace(0, np.pi, contact_samples)
        jump = peak_force * np.sin(t) + rng.normal(0, 5.0, size=contact_samples)
        jump = np.clip(jump, 0, None)
        signal.append(jump)
        signal.append(np.full(flight_samples, 5.0))
    return np.concatenate(signal), fs


def test_profiler_columns_and_diffI():
    """(a) Synthetischer Sprung-Lauf: Contact_t/Integral/diffI vorhanden und konsistent."""
    from scipy.signal import butter

    raw_signal, fs = _make_synthetic_signal()
    b, a = butter(2, 12 / (fs / 2), btype='low')

    result = profiler.analyze_raw_signal(raw_signal, fs, peak_height=2200, peak_distance=500, a=a, b=b)
    assert result["ok"], f"Profiler-Lauf fehlgeschlagen: {result.get('message')}"
    jumps = result["jumps"]
    assert len(jumps) >= 2, f"Zu wenige valide Spruenge fuer den Test ({len(jumps)})."

    for j in jumps:
        for col in ("Contact_t", "Integral", "diffI"):
            assert col in j, f"Spalte '{col}' fehlt im Sprung-Dict."

    assert not np.isfinite(jumps[0]["diffI"]), "diffI des ersten validen Sprungs sollte NaN sein."

    for i in range(1, len(jumps)):
        expected = jumps[i]["Integral"] - jumps[i - 1]["Integral"]
        actual = jumps[i]["diffI"]
        assert np.isfinite(actual), f"diffI bei Sprung {i} sollte nicht NaN sein."
        assert abs(actual - expected) < 1e-9, (
            f"diffI[{i}]={actual} != Integral[{i}]-Integral[{i - 1}]={expected}")

    print("  OK: Contact_t/Integral/diffI vorhanden, diffI[i] == Integral[i]-Integral[i-1].")


def test_compute_jump_score():
    """(b) compute_jump_score: Importance-Summe == 1.0, trend==abs bei gleicher Deltarichtung."""
    feature_order = ["Peak_t", "Peak_Prct", "Explosiv", "preSlope", "postSlope", "Symmetry"]
    reference = {f: 10.0 for f in feature_order}
    deviation = {f: 2.0 for f in feature_order}
    current = {f: 14.0 for f in feature_order}     # current > reference -> delta > 0 ueberall
    importance = {f: 1.0 for f in feature_order}    # wird intern auf Summe 1.0 normiert
    direction = {f: 1 for f in feature_order}        # ueberall "hoeher ist besser"

    result = importance_utils.compute_jump_score(
        current_features=current, reference=reference, deviation=deviation,
        importance=importance, direction=direction, feature_order=feature_order)

    used_importance_sum = sum(d["Importance_norm"] for d in result["details"])
    assert abs(used_importance_sum - 1.0) < 1e-9, f"Importance-Summe != 1.0: {used_importance_sum}"
    assert abs(result["trend_score"] - result["abs_score"]) < 1e-9, (
        f"trend_score ({result['trend_score']}) != abs_score ({result['abs_score']}) "
        f"obwohl alle Deltas gleiche Richtung haben.")

    print("  OK: Importance-Summe == 1.0, trend_score == abs_score bei gleicher Deltarichtung.")


def test_backward_compat_baseline_load():
    """(c) Alte Baseline-CSV ohne "Mode"-Spalte wird geladen (als "halten" interpretiert)."""
    import pandas as pd

    tmp_dir = tempfile.mkdtemp(prefix="hdts_test_")
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_dir)
        shutil.copy(os.path.join(REPO_ROOT, "goldTableNeu.xlsx"), tmp_dir)
        os.makedirs("athleten_daten", exist_ok=True)

        var_names = ["Peak_t", "Peak_Prct", "Explosiv", "preSlope", "postSlope", "Symmetry"]
        old_rows = [{
            "Feature": v, "Median": 10.0, "MAD": 2.0, "Importance": 1.0 / len(var_names),
            "H_Max": 3.9, "HG_Avg": 0.05, "Total_Jumps": 120,
        } for v in var_names]
        pd.DataFrame(old_rows).to_csv(os.path.join("athleten_daten", "oldathlete_baseline.csv"), index=False)

        analyzer = jump_analyzer_module.JumpAnalyzer()
        messages = []
        analyzer.load_profile("oldathlete", logFcn=messages.append)

        assert "halten" in analyzer.profiles and "aufbau" in analyzer.profiles, (
            "Nach dem Laden muessen beide Modi vorhanden sein (halten aus Datei, aufbau als Fallback).")
        assert abs(analyzer.profiles["halten"]["reference"]["Peak_t"] - 10.0) < 1e-9, (
            "Modus 'halten' sollte die Werte aus der alten (Mode-losen) Baseline-CSV uebernehmen.")
        assert any("alten Format" in m for m in messages), (
            "Es sollte ein Log-Hinweis auf das alte Format / die Neuberechnung erscheinen.")

        # Auch profiler.load_scoring_profile muss die alte Datei lesen koennen.
        profiles, h_max, modus = profiler.load_scoring_profile(
            "oldathlete", os.path.join(tmp_dir, "goldTableNeu.xlsx"), n_existing_jumps=100)
        assert "halten" in profiles and "aufbau" in profiles
        assert abs(profiles["halten"]["reference"]["Peak_t"] - 10.0) < 1e-9

        print("  OK: Alte Baseline-CSV ohne 'Mode'-Spalte wird rueckwaertskompatibel geladen.")
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_phase_switch():
    """(d) Phasen-Weiche: h_rel 0.5 -> "aufbau", h_rel 0.95 -> "halten"."""
    h_max = 4.0
    phase_low = importance_utils.determine_phase(0.5 * h_max, h_max)
    phase_high = importance_utils.determine_phase(0.95 * h_max, h_max)
    assert phase_low == "aufbau", f"h_rel=0.5 sollte 'aufbau' ergeben, war '{phase_low}'."
    assert phase_high == "halten", f"h_rel=0.95 sollte 'halten' ergeben, war '{phase_high}'."
    print("  OK: h_rel 0.5 -> 'aufbau', h_rel 0.95 -> 'halten'.")


def test_ampel_logic():
    """(e) 8 Zweige der phasenabhaengigen Feedback-Logik + Aufbau-Fallback.

    Geprueft wird decide_feedback (LED + Text in EINER Funktion): (direction, level)
    UND dass der Text zur Richtung passt (keine Aufbau-Divergenz mehr). classify_ampel
    ist der duenne Wrapper und muss die ersten beiden Werte spiegeln.
    """
    from esp_client import decide_feedback, classify_ampel
    from importance_utils import DIFFI_DEADBAND

    GOOD_DIFFI = DIFFI_DEADBAND + 10.0   # sicher ueber dem diffI-Totband (GRUEN)
    LOW_DIFFI = -3.0                      # unter dem Totband (kein GRUEN im Aufbau)

    def check(args, expected_dir, expected_level, text_contains):
        direction, level, text = decide_feedback(*args[0], **args[1])
        assert (direction, level) == (expected_dir, expected_level), (args, direction, level)
        assert text_contains in text, (text, text_contains)
        # Wrapper muss die ersten beiden Werte identisch liefern.
        assert classify_ampel(*args[0], **args[1]) == (direction, level)

    # --- Phase "halten" ---
    check(((0.2, 1.0), dict(phase="halten")), "GOOD", 0, "stabil")                 # Totband
    check(((1.0, 1.2), dict(phase="halten")), "EARLY", 1, "früher treten")         # Gate ok
    check(((-1.5, 1.6), dict(phase="halten")), "LATE", 2, "später treten")         # Stufe 2
    check(((0.7, 2.0), dict(phase="halten")), "OFF", 0, "uneinheitlich")           # Gate verletzt

    # --- Phase "aufbau" mit individueller Aufbau-Baseline ---
    check(((2.0, 2.5), dict(phase="aufbau", diffI=GOOD_DIFFI, aufbau_reference_ok=True)),
          "GOOD", 0, "Höhe kommt")                                                  # Erfolg schlaegt Muster
    check(((-1.0, 1.2), dict(phase="aufbau", diffI=LOW_DIFFI, aufbau_reference_ok=True)),
          "LATE", 1, "später treten")
    check(((1.0, 1.2), dict(phase="aufbau", diffI=LOW_DIFFI, aufbau_reference_ok=True)),
          "EARLY", 1, "früher treten")
    check(((0.2, 1.0), dict(phase="aufbau", diffI=LOW_DIFFI, aufbau_reference_ok=True)),
          "OFF", 0, "mehr Druck ins Tuch")

    # --- Aufbau-Fallback OHNE individuelle Aufbau-Baseline: nur diffI, NIE Richtung ---
    check(((2.0, 2.5), dict(phase="aufbau", diffI=GOOD_DIFFI, aufbau_reference_ok=False)),
          "GOOD", 0, "Höhe kommt")
    check(((2.0, 2.5), dict(phase="aufbau", diffI=LOW_DIFFI, aufbau_reference_ok=False)),
          "OFF", 0, "kein Signal")
    # Erster Sprung (diffI = NaN) mit Baseline -> GRUEN "erster Sprung". Bewusst kein
    # AUS: die Ampel soll ab der ersten Landung sichtbar sein, nicht defekt wirken.
    check(((1.0, 1.2), dict(phase="aufbau", diffI=float("nan"), aufbau_reference_ok=True)),
          "GOOD", 0, "erster Sprung")

    print("  OK: 8 Zweige (4 je Phase) + Aufbau-Fallback, Text passt jeweils zur LED.")


def test_diffi_deadband():
    """(g) Aufbau GRUEN erst oberhalb des diffI-Totbands (nicht schon bei diffI > 0)."""
    from esp_client import decide_feedback
    from importance_utils import DIFFI_DEADBAND

    # knapp unter dem Totband: KEIN Gruen (faellt auf Richtung/AUS zurueck)
    d_below, _, _ = decide_feedback(0.2, 1.0, phase="aufbau", diffI=DIFFI_DEADBAND - 1.0,
                                    aufbau_reference_ok=True)
    assert d_below != "GOOD", d_below
    # klar darueber: GRUEN
    d_above, _, _ = decide_feedback(0.2, 1.0, phase="aufbau", diffI=DIFFI_DEADBAND + 1.0,
                                    aufbau_reference_ok=True)
    assert d_above == "GOOD", d_above
    print(f"  OK: Aufbau-GRUEN erst ab diffI > {DIFFI_DEADBAND}.")


def test_hysterese_phase():
    """(h) HG-Phasenerkennung mit asymmetrischer Hysterese (Einstieg sofort,
    Ausstieg erst nach mehreren ruhigen Spruengen, kein Flattern)."""
    from importance_utils import HG_AUFBAU_ENTRY, HYSTERESE_EXIT

    a = jump_analyzer_module.JumpAnalyzer()

    def phase_after(hg_value):
        a._hg_series.append(hg_value)
        return a._update_phase_from_hg()

    # Ein klarer Aufbau-Sprung -> sofort "aufbau".
    assert phase_after(HG_AUFBAU_ENTRY + 0.1) == "aufbau"
    # Ein einzelner ruhiger Sprung darf NICHT sofort zurueckschalten (Hysterese haelt).
    assert phase_after(0.0) == "aufbau"
    # Anhaltende Ruhe -> irgendwann zurueck auf "halten" (Fenster leert sich + HYSTERESE_EXIT).
    p = "aufbau"
    for _ in range(6):
        p = phase_after(0.0)
    assert p == "halten", p
    # Erneuter Aufbau-Impuls -> sofort zurueck auf "aufbau".
    assert phase_after(HG_AUFBAU_ENTRY + 0.1) == "aufbau"
    print("  OK: Hysterese-Phase: Einstieg sofort, Ausstieg verzoegert, kein Flattern.")


def test_rolling_reference_and_mad_floor():
    """(i) Rolling-Referenz ab MIN_ROLL Kontakten; davor Warmstart; MAD-Floor greift."""
    from importance_utils import MIN_ROLL, MAD_FLOOR_FACTOR

    var_names = ["Peak_t", "Peak_Prct", "Explosiv", "preSlope", "postSlope", "Symmetry"]
    a = jump_analyzer_module.JumpAnalyzer()
    # Kontrollierte Testbedingungen: bekannte GoldStd + gespeicherte (Warmstart-)Referenz.
    a._gold_std = {v: 10.0 for v in var_names}
    a.profiles["halten"] = {
        "reference": {v: 100.0 for v in var_names},
        "deviation": {v: 0.001 for v in var_names},   # absurd eng -> Floor muss greifen
        "importance_dict": {v: 1.0 / len(var_names) for v in var_names},
    }
    a._roll["halten"].clear()

    # Vor MIN_ROLL: Warmstart aus gespeicherter Baseline, aber Deviation auf den Floor angehoben.
    ref = a._reference_for("halten")
    assert ref["reference"]["Peak_t"] == 100.0
    assert abs(ref["deviation"]["Peak_t"] - MAD_FLOOR_FACTOR * 10.0) < 1e-9, ref["deviation"]["Peak_t"]

    # Fenster mit MIN_ROLL homogenen Kontakten fuellen -> Rolling-Referenz aktiv.
    for _ in range(MIN_ROLL):
        a._roll["halten"].append({v: 50.0 for v in var_names})
    ref2 = a._reference_for("halten")
    assert abs(ref2["reference"]["Peak_t"] - 50.0) < 1e-9, ref2["reference"]["Peak_t"]
    # MAD der homogenen Werte ist 0 -> Floor hebt Deviation ebenfalls auf 0.30*GoldStd.
    assert abs(ref2["deviation"]["Peak_t"] - MAD_FLOOR_FACTOR * 10.0) < 1e-9, ref2["deviation"]["Peak_t"]
    print("  OK: Warmstart<MIN_ROLL, danach Rolling; MAD-Floor = 0.30*GoldStd greift.")


def test_gold_warmstart_green():
    """(j) Gold-Warmstart: GRUEN statt Richtung und statt AUS.

    Empirischer Anlass: in 5 von 7 Athletendateien waren es exakt MIN_ROLL = 6
    GELB-Spruenge im Halten - genau die Kontakte vor dem Fuellen des Rolling-
    Fensters, gescort gegen den Goldstandard (Uebergabe 5.1.3).
    """
    from esp_client import decide_feedback
    from importance_utils import MIN_ROLL, DIFFI_DEADBAND

    # Gegen Gold: nie eine Richtung und nie AUS - auch bei grossem Trend.
    for trend in (2.5, -2.5, 0.1):
        for phase in ("halten", "aufbau"):
            d, lvl, _ = decide_feedback(trend, 2.6, phase=phase, diffI=-5.0,
                                        reference_is_own=False)
            assert (d, lvl) == ("GOOD", 0), (phase, trend, d, lvl)

    # Hoehengewinn schlaegt auch waehrend des Warmstarts durch.
    d, _, txt = decide_feedback(2.5, 2.6, phase="aufbau",
                                diffI=DIFFI_DEADBAND + 10.0, reference_is_own=False)
    assert (d, txt) == ("GOOD", "Höhe kommt"), (d, txt)

    # Mit eigener Referenz kommt die Richtung zurueck (sonst waere alles Dauergruen).
    assert decide_feedback(2.5, 2.6, phase="halten", reference_is_own=True)[0] == "EARLY"

    # Der Analyzer muss die Herkunft der Referenz korrekt melden.
    var_names = ["Peak_t", "Peak_Prct", "Explosiv", "preSlope", "postSlope", "Symmetry"]
    a = jump_analyzer_module.JumpAnalyzer()
    a._gold_std = {v: 10.0 for v in var_names}
    a.profiles["halten"] = {
        "reference": {v: 100.0 for v in var_names},
        "deviation": {v: 5.0 for v in var_names},
        "importance_dict": {v: 1.0 / len(var_names) for v in var_names},
    }
    a._roll["halten"].clear()
    a.mode_sources = {"halten": "Goldstandard"}
    assert a._reference_for("halten")["is_own"] is False, "Gold-Warmstart ist NICHT der eigene Koerper."
    for _ in range(MIN_ROLL):
        a._roll["halten"].append({v: 50.0 for v in var_names})
    assert a._reference_for("halten")["is_own"] is True, "Gefuelltes Rolling-Fenster ist der eigene Koerper."
    a._roll["halten"].clear()
    a.mode_sources = {"halten": "individuelle Baseline"}
    assert a._reference_for("halten")["is_own"] is True, "Individuelle Baseline traegt den Warmstart."

    print("  OK: Gold-Warmstart zeigt GRUEN (nie Richtung, nie AUS); "
          "eigene Referenz schaltet die Richtung frei.")


def test_quantile_deadband():
    """(k) Quantil-Totband (Uebergabe 7.1).

    Ein fixes Totband 0.5 ergibt je Athlet voellig verschiedene Feedback-Raten
    (33 % Live-Test vs. 12 % Validierungsgruppe). Das Quantil-Totband bindet die
    Rate an die eigene Streuung statt an eine fremde Zahl.
    """
    from importance_utils import (effective_deadband, DEADBAND_TREND,
                                  DEADBAND_MIN_N, DEADBAND_WINDOW,
                                  DEADBAND_FLOOR, DEADBAND_CEIL,
                                  DEADBAND_QUANTILE)
    from esp_client import decide_feedback

    # Zu wenig eigene Werte -> Fixwert (kein halbgares Quantil aus 3 Zahlen).
    assert effective_deadband([]) == DEADBAND_TREND
    assert effective_deadband([0.4] * (DEADBAND_MIN_N - 1)) == DEADBAND_TREND

    # Ab DEADBAND_MIN_N greift das Quantil. Bei 0..1 gleichverteilt liegt P70 ~0.7.
    vals = [i / 10.0 for i in range(11)]          # 0.0 .. 1.0
    band = effective_deadband(vals)
    assert abs(band - 0.7) < 0.05, band

    # Vorzeichen egal - gerechnet wird mit Betraegen.
    assert abs(effective_deadband([-v for v in vals]) - band) < 1e-9

    # Zielrate: bei P70 liegen ~30 % der eigenen Werte ueber dem Totband.
    ueber = sum(1 for v in vals if abs(v) > band) / len(vals)
    assert 0.15 <= ueber <= 0.45, ueber

    # Leitplanken greifen in beide Richtungen.
    assert effective_deadband([0.001] * 20) == DEADBAND_FLOOR, "sehr gleichfoermig -> Floor"
    assert effective_deadband([9.0] * 20) == DEADBAND_CEIL, "chaotisch -> Ceil"

    # Nur die juengsten DEADBAND_WINDOW Werte zaehlen (Tagesform, kein Gedaechtnis).
    alt_gross = [9.0] * 50 + [0.2] * DEADBAND_WINDOW
    assert effective_deadband(alt_gross) == DEADBAND_FLOOR, "altes Fenster darf nicht nachwirken"

    # Wirkung in decide_feedback: derselbe trend, zwei Totbaender -> zwei Ampeln.
    assert decide_feedback(0.6, 0.8, phase="halten", deadband=0.30)[0] == "EARLY"
    assert decide_feedback(0.6, 0.8, phase="halten", deadband=1.00)[0] == "GOOD"
    # Ohne Angabe bleibt es beim Fixwert (Rueckwaertskompatibilitaet).
    assert decide_feedback(0.6, 0.8, phase="halten")[0] == "EARLY"
    assert decide_feedback(0.4, 0.8, phase="halten")[0] == "GOOD"

    # Analyzer: Gold-gescorte Kontakte duerfen NICHT ins Totband-Fenster.
    var_names = ["Peak_t", "Peak_Prct", "Explosiv", "preSlope", "postSlope", "Symmetry"]
    a = jump_analyzer_module.JumpAnalyzer()
    assert set(a._trend_hist) == {"aufbau", "halten"}, "Totband-Fenster je Phase"
    assert a._trend_hist["halten"].maxlen == DEADBAND_WINDOW
    a._trend_hist["halten"].extend([0.3] * DEADBAND_MIN_N)
    a.reset()
    assert len(a._trend_hist["halten"]) == 0, "reset() muss das Totband-Fenster leeren"

    print(f"  OK: P{int(DEADBAND_QUANTILE * 100)}-Totband ab {DEADBAND_MIN_N} eigenen Werten, "
          f"Leitplanken {DEADBAND_FLOOR}/{DEADBAND_CEIL}, Fixwert als Rueckfall.")


def test_aufbau_fallback_pipeline():
    """(f) Zu wenig Aufbau-Spruenge: keine Aufbau-Zeilen in der Baseline-CSV,
    Analyzer erkennt den fehlenden Modus als Goldstandard-Quelle."""
    import pandas as pd
    import baseline_manager

    tmp_dir = tempfile.mkdtemp(prefix="hdts_test_")
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_dir)
        shutil.copy(os.path.join(REPO_ROOT, "goldTableNeu.xlsx"), tmp_dir)
        os.makedirs("athleten_daten", exist_ok=True)

        # Steady-State-Athlet: 30 Spruenge, praktisch kein Hoehengewinn (HG ~ 0)
        # -> Modus "aufbau" leer, Modus "halten" gut gefuellt.
        rng = np.random.default_rng(7)
        rows = []
        for _ in range(30):
            rows.append({
                "Peak_t": 0.11 + rng.normal(0, 0.005),
                "Peak_Prct": 45.0 + rng.normal(0, 2.0),
                "Explosiv": 7.0e4 + rng.normal(0, 2000),
                "preSlope": 7.0e4 + rng.normal(0, 2000),
                "postSlope": -6.0e4 + rng.normal(0, 2000),
                "Symmetry": 0.9 + rng.normal(0, 0.05),
                "Height": 3.0 + rng.normal(0, 0.05),
                "HG": rng.normal(0.0, 0.02),
            })
        pd.DataFrame(rows).to_csv(os.path.join("athleten_daten", "steady_all.csv"), index=False)

        msg = baseline_manager.update_athlete_baseline("steady")
        assert "kein Referenzsatz" in msg, f"Statusmeldung unerwartet: {msg}"

        df_b = pd.read_csv(os.path.join("athleten_daten", "steady_baseline.csv"))
        assert set(df_b["Mode"]) == {"halten"}, (
            f"Baseline-CSV sollte NUR 'halten'-Zeilen enthalten, hat: {set(df_b['Mode'])}")

        analyzer = jump_analyzer_module.JumpAnalyzer()
        analyzer.load_profile("steady", logFcn=lambda m: None)
        assert analyzer.mode_sources.get("halten") == "individuelle Baseline"
        assert analyzer.mode_sources.get("aufbau") == "Goldstandard", (
            "Fehlender Aufbau-Modus muss als Goldstandard-Quelle erkannt werden "
            "(-> Ampel sperrt Richtungslichter im Aufbau).")

        print("  OK: Keine Aufbau-Zeilen bei <15 Aufbau-Spruengen, Quelle korrekt erkannt.")
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    tests = [
        ("a) Profiler: Contact_t/Integral/diffI", test_profiler_columns_and_diffI),
        ("b) compute_jump_score", test_compute_jump_score),
        ("c) Rueckwaertskompatible Baseline", test_backward_compat_baseline_load),
        ("d) Phasen-Weiche", test_phase_switch),
        ("e) Feedback-Logik (8 Zweige, Text==LED)", test_ampel_logic),
        ("f) Aufbau-Fallback in der Pipeline", test_aufbau_fallback_pipeline),
        ("g) diffI-Totband (Aufbau-GRUEN)", test_diffi_deadband),
        ("h) HG-Phasenerkennung mit Hysterese", test_hysterese_phase),
        ("i) Rolling-Referenz + MAD-Floor", test_rolling_reference_and_mad_floor),
        ("j) Gold-Warmstart: GRUEN statt Richtung/AUS", test_gold_warmstart_green),
        ("k) Quantil-Totband (7.1)", test_quantile_deadband),
    ]

    failures = 0
    for name, fn in tests:
        print(f"[TEST] {name}")
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"  FAIL: {e}")
        except Exception:
            failures += 1
            print("  FAIL (unerwarteter Fehler):")
            traceback.print_exc()

    print()
    if failures == 0:
        print("ALLE TESTS GRUEN.")
        return 0
    print(f"{failures} Test(s) fehlgeschlagen.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
