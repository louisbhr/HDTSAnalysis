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
    """(e) Alle 8 Zweige der phasenabhaengigen Ampel-Logik + Aufbau-Fallback."""
    from esp_client import classify_ampel

    # --- Phase "halten" (Score gegen Halten-Referenz) ---
    # 1) Totband: |trend| < DEADBAND_TREND (0.5) -> GRUEN, Timing stabil.
    assert classify_ampel(0.2, 1.0, phase="halten") == ("GOOD", 0)
    # 2) trend > +Totband, Gate ok (1.0/1.2 = 0.83 > 0.6) -> GELB "frueher treten".
    assert classify_ampel(1.0, 1.2, phase="halten") == ("EARLY", 1)
    # 3) trend < -Totband, Gate ok (1.5/1.6 = 0.94), abs 1.6 > 1.4 -> Stufe 2, BLAU.
    assert classify_ampel(-1.5, 1.6, phase="halten") == ("LATE", 2)
    # 4) Gate verletzt (0.7/2.0 = 0.35 <= 0.6) -> AUS.
    assert classify_ampel(0.7, 2.0, phase="halten") == ("OFF", 0)

    # --- Phase "aufbau" (Score gegen individuelle Aufbau-Referenz) ---
    # 5) Erfolg schlaegt Muster: diffI > 0 -> GRUEN, auch bei grosser Abweichung.
    assert classify_ampel(2.0, 2.5, phase="aufbau", diffI=5.0,
                          aufbau_reference_ok=True) == ("GOOD", 0)
    # 6) diffI <= 0 und trend < -Totband -> BLAU "spaeter/laenger treten".
    assert classify_ampel(-1.0, 1.2, phase="aufbau", diffI=-3.0,
                          aufbau_reference_ok=True) == ("LATE", 1)
    # 7) diffI <= 0 und trend > +Totband -> GELB "frueher treten".
    assert classify_ampel(1.0, 1.2, phase="aufbau", diffI=-3.0,
                          aufbau_reference_ok=True) == ("EARLY", 1)
    # 8) diffI <= 0 und |trend| < Totband -> AUS.
    assert classify_ampel(0.2, 1.0, phase="aufbau", diffI=-3.0,
                          aufbau_reference_ok=True) == ("OFF", 0)

    # --- Aufbau-Fallback ohne individuelle Aufbau-Baseline ---
    # Nur diffI-Kriterium: GRUEN bei diffI > 0, sonst AUS - NIE Richtungslichter.
    assert classify_ampel(2.0, 2.5, phase="aufbau", diffI=5.0,
                          aufbau_reference_ok=False) == ("GOOD", 0)
    assert classify_ampel(2.0, 2.5, phase="aufbau", diffI=-3.0,
                          aufbau_reference_ok=False) == ("OFF", 0)
    # Erster Sprung (diffI = NaN): keine Aussage moeglich -> AUS.
    assert classify_ampel(1.0, 1.2, phase="aufbau", diffI=float("nan"),
                          aufbau_reference_ok=True) == ("OFF", 0)

    print("  OK: 8 Ampel-Zweige (4 je Phase) + Aufbau-Fallback + NaN-diffI korrekt.")


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
        ("e) Ampel-Logik (8 Zweige + Fallback)", test_ampel_logic),
        ("f) Aufbau-Fallback in der Pipeline", test_aufbau_fallback_pipeline),
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
