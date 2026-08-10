#!/usr/bin/env python3
"""
tools/simulate_feedback.py

Offline-Auswertung der NEUEN Ampel-/Feedback-Logik (Scope B) auf echten Sprungdaten.

Es wird KEIN Verhalten nachgebaut: das Skript treibt die Produktionspfade
(JumpAnalyzer-Phasenerkennung + Rolling-Referenz, importance_utils.compute_jump_score,
esp_client.decide_feedback) je Sprung von aussen an und zaehlt aus, was Kind und Trainer
sehen wuerden. Ziel: pruefen, ob die Logik plausibel ist (weder Dauergruen noch Dauerrot,
kein Phasen-Flattern, symmetrischer Trend) und die Parameter (Totband, diffI=22,
HG-Schwellen) an echten Daten kalibrieren.

Datenquellen (Endung entscheidet):
  * .csv  - Pro-Sprung-Tabelle im _all.csv-Schema (6 Features + HG, Height, Integral, diffI)
  * .xlsx - z.B. hdts_knie_gesamt.xlsx (Spalten werden tolerant gemappt)
  * .npz  - gespeicherte Session (Rohsignal) -> profiler.analyze_raw_signal liefert die
            Pro-Sprung-Features. Mit --live-check wird zusaetzlich der echte Live-Pfad
            (JumpAnalyzer.process, 48er-Bloecke) gegengeprueft.

Aufruf:
    python tools/simulate_feedback.py <pfad|ordner> [--profile NAME] [--live-check] [--csv OUT]

--profile steuert den Warmstart der Rolling-Referenz (Default "global" = Goldstandard;
oder ein Athletenname, falls eine Baseline unter athleten_daten/ existiert).
"""
import os
import sys
import glob
import argparse

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from importance_utils import compute_jump_score
import jump_analyzer as jump_analyzer_module
import profiler

try:
    from esp_client import decide_feedback
except Exception:  # pragma: no cover - esp_client sollte immer importierbar sein
    decide_feedback = None

SCORE_FEATURES = ["Peak_t", "Peak_Prct", "Explosiv", "preSlope", "postSlope", "Symmetry"]


# --------------------------------------------------------------------------- #
#  Loader: liefern je eine Liste von Pro-Sprung-Feature-dicts
# --------------------------------------------------------------------------- #
def _rows_from_dataframe(df):
    """Baut Pro-Sprung-dicts (6 Features + HG + diffI) aus einem DataFrame.

    Tolerant: fehlendes diffI wird aus Integral rekonstruiert, fehlendes HG aus Height.
    """
    missing = [f for f in SCORE_FEATURES if f not in df.columns]
    if missing:
        raise ValueError(f"Spalten fehlen: {missing}. Vorhanden: {list(df.columns)}")

    if "diffI" not in df.columns:
        df = df.copy()
        df["diffI"] = df["Integral"].diff() if "Integral" in df.columns else np.nan
    if "HG" not in df.columns:
        df = df.copy()
        df["HG"] = df["Height"].diff() if "Height" in df.columns else np.nan

    rows = []
    for _, r in df.iterrows():
        row = {f: float(r[f]) for f in SCORE_FEATURES}
        row["HG"] = float(r["HG"]) if pd.notna(r.get("HG", np.nan)) else np.nan
        row["diffI"] = float(r["diffI"]) if pd.notna(r.get("diffI", np.nan)) else np.nan
        rows.append(row)
    return rows


def load_csv(path):
    return _rows_from_dataframe(pd.read_csv(path))


def load_xlsx(path):
    return _rows_from_dataframe(pd.read_excel(path))


def load_npz(path):
    """Rohsignal aus der Session -> analyze_raw_signal -> Pro-Sprung-dicts."""
    data = np.load(path, allow_pickle=True)
    if "raw_signal" not in data:
        raise ValueError(f"'raw_signal' fehlt in {os.path.basename(path)}.")
    raw = np.asarray(data["raw_signal"], dtype=float).flatten()
    fs = float(data["fs"]) if "fs" in data else 1000.0
    a = jump_analyzer_module.JumpAnalyzer()   # nur fuer Filter/Peak-Parameter
    result = profiler.analyze_raw_signal(
        raw, fs, a.peak_height, a.peak_distance, a.a, a.b)
    if not result.get("ok"):
        raise ValueError(result.get("message", "analyze_raw_signal fehlgeschlagen"))
    return result["jumps"]


def load_rows(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return load_csv(path)
    if ext == ".xlsx":
        return load_xlsx(path)
    if ext == ".npz":
        return load_npz(path)
    raise ValueError(f"Unbekannte Endung: {ext}")


# --------------------------------------------------------------------------- #
#  Entscheidungssimulation (nutzt die echten Produktionspfade)
# --------------------------------------------------------------------------- #
def simulate(rows, profile="global"):
    """Treibt Phasenerkennung + Rolling-Referenz + decide_feedback je Sprung an.

    Rueckgabe: Liste von Records {phase, direction, level, trend, abs, text}.
    """
    if decide_feedback is None:
        raise RuntimeError("esp_client.decide_feedback nicht importierbar.")

    a = jump_analyzer_module.JumpAnalyzer()
    a.load_profile(profile, logFcn=lambda *_: None)
    a.reset()
    a.load_profile(profile, logFcn=lambda *_: None)  # reset() laesst Profil unberuehrt; sicherheitshalber
    aufbau_ok = (a.mode_sources.get("aufbau") == "individuelle Baseline")

    records = []
    prev_hg = None
    for row in rows:
        # Phase aus der HG-Dynamik: der ueber den VORIGEN Kontakt abgeschlossene HG
        # ist bei diesem Sprung bekannt (Ein-Sprung-Versatz wie im Live-Pfad).
        if prev_hg is not None and np.isfinite(prev_hg):
            a._hg_series.append(prev_hg)
        phase = a._update_phase_from_hg()

        cf = {f: row[f] for f in a.var_names}
        ref = a._reference_for(phase)
        result = compute_jump_score(
            current_features=cf,
            reference=ref["reference"],
            deviation=ref["deviation"],
            importance=ref["importance_dict"],
            direction=a.direction_multiplier,
            feature_order=a.var_names,
        )
        trend, absx = result["trend_score"], result["abs_score"]
        direction, level, text = decide_feedback(
            trend, absx, phase=phase, diffI=row.get("diffI", np.nan),
            aufbau_reference_ok=aufbau_ok)

        records.append({"phase": phase, "direction": direction, "level": level,
                        "trend": trend, "abs": absx, "text": text})
        a._roll[phase].append(cf)
        prev_hg = row.get("HG", np.nan)
    return records


def live_check(path, profile="global"):
    """Faithfulness: Rohsignal durch den echten Live-Pfad (process, 48er-Bloecke)
    schicken und die LED-Richtungsfolge zurueckgeben (nur fuer .npz sinnvoll)."""
    data = np.load(path, allow_pickle=True)
    raw = np.asarray(data["raw_signal"], dtype=float).flatten()
    a = jump_analyzer_module.JumpAnalyzer()
    a.load_profile(profile, logFcn=lambda *_: None)
    a.reset()
    a.load_profile(profile, logFcn=lambda *_: None)
    seq = []
    a.set_on_jump(lambda d: seq.append((d["ampel_direction"], d["ampel_level"])))
    bid = 0
    for start in range(0, len(raw), 48):
        a.process(raw[start:start + 48], bid, logFcn=lambda *_: None)
        bid += 1
    return seq


# --------------------------------------------------------------------------- #
#  Report
# --------------------------------------------------------------------------- #
def _pct(n, total):
    return (100.0 * n / total) if total else 0.0


def summarize(records):
    n = len(records)
    if n == 0:
        return {"n": 0}
    phases = [r["phase"] for r in records]
    dirs = [r["direction"] for r in records]
    n_auf = phases.count("aufbau")
    n_hal = phases.count("halten")
    switches = sum(1 for i in range(1, n) if phases[i] != phases[i - 1])
    lit = sum(1 for d in dirs if d != "OFF")

    hal = [r for r in records if r["phase"] == "halten"]
    auf = [r for r in records if r["phase"] == "aufbau"]

    def count(recs, d):
        return sum(1 for r in recs if r["direction"] == d)

    trends = np.array([r["trend"] for r in records], dtype=float)
    absv = np.array([r["abs"] for r in records], dtype=float)

    return {
        "n": n, "n_aufbau": n_auf, "n_halten": n_hal, "switches": switches,
        "lit_pct": _pct(lit, n),
        "hal_n": len(hal),
        "hal_good": _pct(count(hal, "GOOD"), len(hal)),
        "hal_early": _pct(count(hal, "EARLY"), len(hal)),
        "hal_late": _pct(count(hal, "LATE"), len(hal)),
        "hal_off": _pct(count(hal, "OFF"), len(hal)),
        "auf_n": len(auf),
        "auf_good": _pct(count(auf, "GOOD"), len(auf)),
        "auf_dir": _pct(count(auf, "EARLY") + count(auf, "LATE"), len(auf)),
        "auf_off": _pct(count(auf, "OFF"), len(auf)),
        "trend_median": float(np.median(trends)),
        "trend_neg_pct": _pct(int(np.sum(trends < 0)), n),
        "abs_median": float(np.median(absv)),
    }


def print_report(label, s):
    if s.get("n", 0) == 0:
        print(f"\n=== {label} ===\n  (keine Spruenge)")
        return
    print(f"\n=== {label} ===")
    print(f"  Spruenge: {s['n']}  |  Phasen: {s['n_aufbau']} Aufbau / {s['n_halten']} Halten"
          f"  |  Phasenwechsel: {s['switches']}")
    print(f"  Leuchtquote (Licht an): {s['lit_pct']:.0f}%")
    print(f"  Halten (n={s['hal_n']}): gruen {s['hal_good']:.0f}% | gelb {s['hal_early']:.0f}%"
          f" | blau {s['hal_late']:.0f}% | aus {s['hal_off']:.0f}%")
    print(f"  Aufbau (n={s['auf_n']}): Hoehe kommt {s['auf_good']:.0f}% | Richtung {s['auf_dir']:.0f}%"
          f" | aus {s['auf_off']:.0f}%")
    print(f"  trend: Median {s['trend_median']:+.2f}, {s['trend_neg_pct']:.0f}% negativ"
          f"  |  abs: Median {s['abs_median']:.2f}")


def main():
    ap = argparse.ArgumentParser(description="Simuliert die neue Feedback-Logik auf Sprungdaten.")
    ap.add_argument("path", help="Datei (.csv/.xlsx/.npz) oder Ordner")
    ap.add_argument("--profile", default="global", help="Warmstart-Profil (Default: global/Gold)")
    ap.add_argument("--live-check", action="store_true",
                    help="Fuer .npz zusaetzlich den echten Live-Pfad gegenpruefen")
    ap.add_argument("--csv", default=None, help="Kennzahlen zusaetzlich als CSV-Zeilen hierhin")
    args = ap.parse_args()

    # Pfade absolut aufloesen, dann ins Repo wechseln, damit load_profile die
    # goldTableNeu.xlsx und Athleten-Baselines unter athleten_daten/ findet.
    args.path = os.path.abspath(args.path)
    if args.csv is not None:
        args.csv = os.path.abspath(args.csv)
    os.chdir(REPO_ROOT)

    if os.path.isdir(args.path):
        files = sorted(glob.glob(os.path.join(args.path, "**", "*.*"), recursive=True))
        files = [f for f in files if os.path.splitext(f)[1].lower() in (".csv", ".xlsx", ".npz")]
    else:
        files = [args.path]
    if not files:
        print("Keine passenden Dateien (.csv/.xlsx/.npz) gefunden.")
        return 1

    all_records = []
    csv_rows = []
    for path in files:
        label = os.path.basename(path)
        try:
            rows = load_rows(path)
            records = simulate(rows, profile=args.profile)
        except Exception as e:
            print(f"\n=== {label} ===\n  FEHLER: {e}")
            continue
        all_records.extend(records)
        s = summarize(records)
        print_report(label, s)

        if args.live_check and path.lower().endswith(".npz"):
            try:
                live_seq = live_check(path, profile=args.profile)
                sim_seq = [(r["direction"], r["level"]) for r in records]
                m = min(len(live_seq), len(sim_seq))
                mism = sum(1 for i in range(m) if live_seq[i] != sim_seq[i])
                # Grobe Gegenprobe: Live-Kontakterkennung (inkrementell) und die
                # Offline-Segmentierung koennen an den Raendern um 1-2 Spruenge
                # abweichen -> kleine Differenzen sind normal, grosse ein Warnsignal.
                print(f"  live-check: process()={len(live_seq)} Spruenge, sim={len(sim_seq)}; "
                      f"Richtungsabweichungen (erste {m}): {mism}")
            except Exception as e:
                print(f"  live-check FEHLER: {e}")

        if args.csv is not None:
            csv_rows.append({"file": label, **s})

    if len(files) > 1:
        print_report("GESAMT (alle Dateien)", summarize(all_records))

    if args.csv is not None and csv_rows:
        pd.DataFrame(csv_rows).to_csv(args.csv, index=False)
        print(f"\nKennzahlen als CSV gesichert: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
