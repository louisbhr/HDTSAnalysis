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
import re
import sys
import glob
import argparse

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from importance_utils import (compute_jump_score, DEADBAND_TREND, DIFFI_DEADBAND,
                              ROLL_N, MIN_ROLL, effective_deadband,
                              DEADBAND_MODE, DEADBAND_QUANTILE, DEADBAND_WINDOW,
                              DEADBAND_MIN_N)
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


# Einheiten-Suffix im Spaltennamen: "Peak_t [s]" -> "Peak_t", "Höhe [m]" -> "Höhe"
_UNIT_SUFFIX = re.compile(r"\s*\[[^\]]*\]\s*$")
_COLUMN_ALIASES = {"Höhe": "Height", "Hoehe": "Height"}
# Spalten, nach denen sinnvoll gruppiert werden kann (erste Treffer gewinnt).
GROUP_CANDIDATES = ("Athlet", "Serie", "Person", "Athlete")


def _normalize_columns(df):
    """Entfernt Einheiten-Suffixe und mappt deutsche Spaltennamen auf die internen."""
    ren = {}
    for c in df.columns:
        name = _UNIT_SUFFIX.sub("", str(c)).strip()
        ren[c] = _COLUMN_ALIASES.get(name, name)
    return df.rename(columns=ren)


def load_frame(path):
    """Liest .csv/.xlsx als DataFrame mit normalisierten Spaltennamen.

    Die Validierungstabelle hat eine ZWEIZEILIGE Kopfzeile (Gruppen HDTS/Video/Knie
    ueber den echten Namen); deshalb wird Kopfzeile 0 und 1 probiert und die genommen,
    in der die Score-Features auftauchen.
    """
    ext = os.path.splitext(path)[1].lower()
    last = None
    for header in (0, 1):
        if ext == ".csv":
            df = _normalize_columns(pd.read_csv(path, header=header))
        else:
            df = _normalize_columns(pd.read_excel(path, header=header))
        if all(f in df.columns for f in SCORE_FEATURES):
            return df
        last = df
    return last   # nichts gefunden -> _rows_from_dataframe wirft die klare Fehlermeldung


def load_csv(path):
    return _rows_from_dataframe(load_frame(path))


def load_xlsx(path):
    return _rows_from_dataframe(load_frame(path))


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


def load_groups(path, group_by="auto"):
    """Liefert [(gruppenname|None, rows), ...] - je Gruppe eine eigene Simulation.

    Wichtig fuer die Korrektheit: jede Gruppe bekommt spaeter einen EIGENEN Analyzer,
    damit die Rolling-Referenz nicht ueber Athletengrenzen mischt. Die Zwischen-
    Athleten-Streuung ist laut Uebergabe 3.7 das 2.6- bis 10.4-Fache der Within-
    Streuung; ein vermischtes Fenster mittelt ueber fremde Koerper und verschiebt
    trend und abs systematisch. Auch HG/diffI werden so je Gruppe differenziert und
    nicht ueber den Wechsel hinweg.
    """
    if os.path.splitext(path)[1].lower() == ".npz":
        return [(None, load_npz(path))]

    df = load_frame(path)
    col = None
    if group_by == "auto":
        col = next((c for c in GROUP_CANDIDATES if c in df.columns), None)
    elif group_by:
        col = group_by if group_by in df.columns else None
    if col is None:
        return [(None, _rows_from_dataframe(df))]
    return [(str(key), _rows_from_dataframe(grp))
            for key, grp in df.groupby(col, sort=False)]


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
        # Wie im Live-Pfad: Richtung nur gegen die EIGENE Referenz (Rolling-Fenster
        # oder individuelle Baseline); gegen Gold zeigt die Ampel GRUEN.
        ref_is_own = bool(ref.get("is_own", True))
        # Totband aus der eigenen juengsten trend-Verteilung (Uebergabe 7.1) -
        # exakt wie im Live-Pfad, damit die Simulation nicht auseinanderlaeuft.
        band = effective_deadband(a._trend_hist[phase])
        direction, level, text = decide_feedback(
            trend, absx, phase=phase, diffI=row.get("diffI", np.nan),
            aufbau_reference_ok=ref_is_own, reference_is_own=ref_is_own,
            deadband=band)

        records.append({"phase": phase, "direction": direction, "level": level,
                        "trend": trend, "abs": absx, "text": text,
                        "deadband": band})
        a._roll[phase].append(cf)
        if ref_is_own and np.isfinite(trend):
            a._trend_hist[phase].append(float(trend))
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
    # "gruen" statt "Hoehe kommt": GRUEN deckt im Aufbau mehrere Faelle ab
    # (Hoehengewinn, erster Sprung, Gold-Warmstart).
    print(f"  Aufbau (n={s['auf_n']}): gruen {s['auf_good']:.0f}% | Richtung {s['auf_dir']:.0f}%"
          f" | aus {s['auf_off']:.0f}%")
    print(f"  trend: Median {s['trend_median']:+.2f}, {s['trend_neg_pct']:.0f}% negativ"
          f"  |  abs: Median {s['abs_median']:.2f}")


def resolve_profile(arg_profile, path, gname=None):
    """Warmstart-Profil je Datei/Serie bestimmen.

    Nur bei --profile auto wird geraten; sonst gilt der uebergebene Name fuer
    alles. "auto" leitet den Athletennamen aus dem Kontext ab und prueft, ob die
    Baseline wirklich existiert - sonst faellt es auf "global" (Gold) zurueck.

    Der Grund fuer diese Option: ein einzelnes --profile ueber einen ganzen
    Ordner wuerde JEDEN Athleten gegen die Baseline EINES Koerpers warmstarten.
    Die Zwischen-Athleten-Streuung ist laut Uebergabe 3.7 das 2.6- bis 10.4-Fache
    der Within-Streuung - dieser Lauf waere unbrauchbar.

    Kandidaten in dieser Reihenfolge:
      1. Gruppenname (Spalte Athlet/Serie) - bei der Validierungs-xlsx
      2. Ordnername bei athleten_daten/sessions/<name>/*.npz
      3. Dateiname ohne die Endung "_all"
    """
    if str(arg_profile).lower() != "auto":
        return arg_profile

    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.endswith("_all"):
        stem = stem[:-4]

    candidates = []
    if gname:
        candidates.append(str(gname))
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if parent and parent not in ("athleten_daten", "data", "sessions"):
        candidates.append(parent)
    candidates.append(stem)

    for c in candidates:
        if os.path.exists(os.path.join("athleten_daten", f"{c}_baseline.csv")):
            return c
    return "global"


def main():
    ap = argparse.ArgumentParser(description="Simuliert die neue Feedback-Logik auf Sprungdaten.")
    ap.add_argument("path", help="Datei (.csv/.xlsx/.npz) oder Ordner")
    ap.add_argument("--profile", default="global",
                    help="Warmstart-Profil (Default: global/Gold; 'auto' waehlt je "
                         "Datei/Serie die passende Baseline, sonst global)")
    ap.add_argument("--live-check", action="store_true",
                    help="Fuer .npz zusaetzlich den echten Live-Pfad gegenpruefen")
    ap.add_argument("--csv", default=None, help="Kennzahlen zusaetzlich als CSV-Zeilen hierhin")
    ap.add_argument("--group-by", default="auto",
                    help="Spalte, bei der die Referenz neu startet (Default: auto -> "
                         "Athlet/Serie, falls vorhanden; 'none' schaltet die Trennung ab)")
    args = ap.parse_args()
    if str(args.group_by).lower() in ("none", "off", ""):
        args.group_by = None

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

    # "<name>.csv" ist die auf HG > HG_QUALITY_THRESHOLD gefilterte Baseline-Teilmenge
    # von "<name>_all.csv": reine Lade-Kontakte, keine Session. Wer sie mitzaehlt, zaehlt
    # dieselben Spruenge doppelt UND zieht den Schnitt nach oben (Aufbau-Paradox). Liegt
    # das Gegenstueck daneben, wird die gefilterte Datei uebersprungen.
    stems = {os.path.splitext(f)[0] for f in files}
    skipped = [f for f in files if f.lower().endswith(".csv")
               and os.path.splitext(f)[0] + "_all" in stems]
    files = [f for f in files if f not in skipped]

    print("=" * 74)
    print(f"Warmstart-Profil: {args.profile}   |   Gruppierung: {args.group_by or 'aus'}")
    print(f"Konstanten: DEADBAND_TREND={DEADBAND_TREND}  DIFFI_DEADBAND={DIFFI_DEADBAND}"
          f"  ROLL_N={ROLL_N}  MIN_ROLL={MIN_ROLL}")
    if DEADBAND_MODE == "quantil":
        print(f"Totband: quantilbasiert (P{int(DEADBAND_QUANTILE * 100)} der letzten "
              f"{DEADBAND_WINDOW} eigenen trend-Werte, ab {DEADBAND_MIN_N} Werten; "
              f"davor fix {DEADBAND_TREND})")
    else:
        print(f"Totband: fix {DEADBAND_TREND}")
    print(f"Ausgewertete Dateien ({len(files)}):")
    for f in files:
        print(f"    {os.path.relpath(f, REPO_ROOT)}")
    for f in skipped:
        print(f"  ! uebersprungen (gefilterte Baseline-Teilmenge): {os.path.basename(f)}")
    print("=" * 74)

    all_records = []
    csv_rows = []
    for path in files:
        base = os.path.basename(path)
        try:
            groups = load_groups(path, group_by=args.group_by)
        except Exception as e:
            print(f"\n=== {base} ===\n  FEHLER: {e}")
            continue

        records = []
        for gname, rows in groups:
            label = base if gname is None else f"{base} · {gname}"
            prof = resolve_profile(args.profile, path, gname)
            if str(args.profile).lower() == "auto":
                # Transparent machen, wogegen warmgestartet wurde - "global" heisst
                # Gold-Warmstart und damit MIN_ROLL gruene Kontakte je Phase.
                label = f"{label}  [Profil: {prof}]"
            try:
                grp_records = simulate(rows, profile=prof)
            except Exception as e:
                print(f"\n=== {label} ===\n  FEHLER: {e}")
                continue
            records.extend(grp_records)
            s = summarize(grp_records)
            print_report(label, s)
            if args.csv is not None:
                csv_rows.append({"file": label, **s})

        if len(groups) > 1 and records:
            print_report(f"{base} — alle Gruppen zusammen", summarize(records))
        all_records.extend(records)

        if args.live_check and path.lower().endswith(".npz"):
            try:
                live_seq = live_check(path, profile=resolve_profile(args.profile, path))
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

    if len(csv_rows) > 1 or len(files) > 1:
        print_report(f"GESAMT ({len(files)} Datei(en))", summarize(all_records))

    if args.csv is not None and csv_rows:
        pd.DataFrame(csv_rows).to_csv(args.csv, index=False)
        print(f"\nKennzahlen als CSV gesichert: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
