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


def main():
    tests = [
        ("a) Profiler: Contact_t/Integral/diffI", test_profiler_columns_and_diffI),
        ("b) compute_jump_score", test_compute_jump_score),
        ("c) Rueckwaertskompatible Baseline", test_backward_compat_baseline_load),
        ("d) Phasen-Weiche", test_phase_switch),
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
