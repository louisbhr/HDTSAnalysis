import os
import numpy as np
import pandas as pd
from scipy.signal import find_peaks, lfilter, lfilter_zi
from scipy.integrate import trapezoid

from importance_utils import compute_jump_score, normalize_importance, determine_phase

# Spalten der vollstaendigen "_all.csv" (alle validen Spruenge).
# "trend_score" und "abs_score" stehen direkt hinter "HG", die neuen Ampel-
# Praediktoren "Contact_t"/"Integral"/"diffI" stehen direkt hinter "abs_score".
ALL_COLUMNS = [
    "Peak", "Peak_t", "Peak_Prct", "timing", "Explosiv",
    "preSlope", "postSlope", "Symmetry", "Height", "HG", "trend_score", "abs_score",
    "Contact_t", "Integral", "diffI",
    "left_idx", "right_idx", "peak_idx", "flight_start_idx", "flight_end_idx",
    "session_id", "timestamp",
]

# Spalten der Baseline-relevanten "<name>.csv" (nur hochwertige Spruenge, HG > HG_QUALITY).
BASELINE_COLUMNS = [
    "Peak", "Peak_t", "Peak_Prct", "timing", "Explosiv",
    "preSlope", "postSlope", "Symmetry", "Height", "HG", "trend_score", "abs_score",
    "Contact_t", "Integral", "diffI",
]

# Schwelle, ab der ein Sprung als "hochwertig" / baseline-relevant gilt.
HG_QUALITY_THRESHOLD = 0.1

# Ab wie vielen bereits vorhandenen Spruengen in der "<name>.csv" auf die individuelle
# Baseline (statt Goldstandard) gescort wird. Spiegelt die Schwelle im BaselineManager (>50).
GOLD_TO_BASELINE_THRESHOLD = 50

# Score-relevante Features. "Peak" (totes Feature, nie in current_features uebergeben)
# und "timing" (exakt 100*Peak_t, perfekte Kollinearitaet mit Peak_Prct) sind raus
# (Validierungsstudie). SCORE_VAR_NAMES (Lookup in Baseline/Goldstandard) und
# SCORE_FEATURES (tatsaechliche Score-Eingabe) sind dadurch identisch - als zwei
# Namen belassen, weil sie unterschiedliche Rollen dokumentieren.
SCORE_VAR_NAMES = ["Peak_t", "Peak_Prct", "Explosiv",
                   "preSlope", "postSlope", "Symmetry"]

SCORE_FEATURES = ["Peak_t", "Peak_Prct", "Explosiv",
                  "preSlope", "postSlope", "Symmetry"]

# Korrekturrichtung je Feature - identisch zum JumpAnalyzer, damit der Score konsistent ist.
DIRECTION_MULTIPLIER = {
    "Peak_t": 1, "Peak_Prct": 1,
    "Symmetry": 1, "postSlope": 1, "preSlope": -1, "Explosiv": -1,
}


def analyze_raw_signal(raw_signal, fs, peak_height, peak_distance, a, b):
    """Analysiert ein Roh-Kraftsignal und liefert alle berechneten Groessen zurueck.

    Diese Funktion ist die EINZIGE Berechnungslogik des Profilers und wird sowohl
    fuer die CSV-Erzeugung (run_offline_profiler) als auch fuer die Session-Speicherung
    (session_storage.save_session) verwendet.
    """
    signal_array = np.asarray(raw_signal, dtype=float).flatten()

    empty = {
        "filtered_signal": np.array([]), "raw_signal": signal_array,
        "peaks": np.array([], dtype=int), "left_idx": np.array([], dtype=int),
        "right_idx": np.array([], dtype=int), "heights": np.array([]),
        "hg": np.array([]), "jumps": [], "ok": False,
    }

    if signal_array.size < 2:
        empty["message"] = "Profiler: Signal zu kurz fuer eine Analyse."
        return empty

    # ---- 1. Filtern, Peaks, Kontaktphasen ----
    zi_fresh = lfilter_zi(b, a) * signal_array[0]
    filt_sig, _ = lfilter(b, a, signal_array, zi=zi_fresh)
    peaks, props = find_peaks(filt_sig, height=peak_height, distance=peak_distance)

    if len(peaks) == 0:
        empty["filtered_signal"] = filt_sig
        empty["message"] = "Profiler: Keine Spruenge in den Rohdaten gefunden."
        return empty

    left_idx, right_idx = [], []
    threshold_factor = 0.05
    window = int(0.25 * fs)

    for i, idx in enumerate(peaks):
        pks = props["peak_heights"][i]
        threshold = threshold_factor * pks

        left_bound = round((peaks[i - 1] + idx) / 2) if i > 0 else max(0, idx - window)
        wL = max(left_bound, idx - window)
        segL = filt_sig[wL:idx + 1]
        below = np.where(segL < threshold)[0]
        idx_start = wL + (below[-1] if len(below) > 0 else int(np.argmin(np.abs(segL))))

        wR = min(len(filt_sig), idx + window)
        segR = filt_sig[idx:wR]
        below_r = np.where(segR < threshold)[0]
        idx_end = idx + (below_r[0] if len(below_r) > 0 else int(np.argmin(np.abs(segR))))

        left_idx.append(int(idx_start))
        right_idx.append(int(idx_end))

    # ---- 2. Absolute Flughoehen (Flug NACH Sprung i: right_idx[i] -> left_idx[i+1]) ----
    g = 9.81
    absolute_heights = []
    for i in range(len(peaks) - 1):
        flugzeit_samples = left_idx[i + 1] - right_idx[i]
        t_flug = max(0.0, flugzeit_samples) / fs
        absolute_heights.append(0.125 * g * (t_flug ** 2))

    hg_per_jump = np.full(len(peaks), np.nan)
    for i in range(1, len(absolute_heights)):
        hg_per_jump[i] = absolute_heights[i] - absolute_heights[i - 1]

    # ---- 3. Features pro validem Sprung berechnen ----
    jumps = []
    last_integral = None
    for i in range(1, len(absolute_heights)):
        hg = absolute_heights[i] - absolute_heights[i - 1]
        # Flughoehe VOR diesem Kontakt (= Flug NACH dem vorherigen Kontakt). Wird nur
        # fuer die Phasen-Weiche beim Scoring gebraucht, nicht persistiert (siehe
        # ALL_COLUMNS/BASELINE_COLUMNS - das Reindexing dort verwirft unbekannte Keys).
        h_entering = absolute_heights[i - 1]

        left = left_idx[i]
        right = right_idx[i]
        if right <= left:
            continue
        jump = filt_sig[left:right]
        idx_peak = peaks[i] - left

        if len(jump) < 2 or idx_peak <= 1 or idx_peak >= len(jump):
            continue

        peak_t = (peaks[i] / fs) - (left / fs)
        contact_t = len(jump) / fs
        peak_prct = 100 * peak_t / contact_t if contact_t > 0 else np.nan

        t_seg = np.arange(left, right) / fs
        pre_slope_val = (np.polyfit(t_seg[:idx_peak] - t_seg[0], jump[:idx_peak], 1)[0]
                         if len(jump[:idx_peak]) >= 2 else np.nan)
        post_slope_val = (np.polyfit(t_seg[idx_peak:] - t_seg[idx_peak], jump[idx_peak:], 1)[0]
                          if len(jump[idx_peak:]) >= 2 else np.nan)

        pre_int = trapezoid(jump[:idx_peak], t_seg[:idx_peak])
        post_int = trapezoid(jump[idx_peak:], t_seg[idx_peak:])
        symmetry_val = pre_int / post_int if abs(post_int) > 1e-4 else np.nan

        # Integral ueber den GESAMTEN Kontakt + diffI (latenzfreier Praediktor fuer HG,
        # am Kontaktende verfuegbar - noch bevor HG beim naechsten Kontakt messbar ist).
        integral_val = trapezoid(jump, t_seg)
        diffI_val = np.nan if last_integral is None else integral_val - last_integral
        last_integral = integral_val

        jumps.append({
            "Peak": float(props["peak_heights"][i]),
            "Peak_t": float(peak_t),
            "Peak_Prct": float(peak_prct),
            "timing": float(peak_prct * contact_t) if np.isfinite(peak_prct) else np.nan,
            "Explosiv": float(props["peak_heights"][i] / peak_t) if peak_t > 0 else np.nan,
            "preSlope": float(pre_slope_val),
            "postSlope": float(post_slope_val),
            "Symmetry": float(symmetry_val),
            "Height": float(absolute_heights[i]),
            "HG": float(hg),
            "Contact_t": float(contact_t),
            "Integral": float(integral_val),
            "diffI": float(diffI_val),
            "left_idx": int(left),
            "right_idx": int(right),
            "peak_idx": int(peaks[i]),
            "flight_start_idx": int(right_idx[i]),
            "flight_end_idx": int(left_idx[i + 1]),
            "h_entering": float(h_entering),
        })

    return {
        "filtered_signal": filt_sig,
        "raw_signal": signal_array,
        "peaks": np.asarray(peaks, dtype=int),
        "left_idx": np.asarray(left_idx, dtype=int),
        "right_idx": np.asarray(right_idx, dtype=int),
        "heights": np.asarray(absolute_heights, dtype=float),
        "hg": hg_per_jump,
        "jumps": jumps,
        "ok": True,
        "message": f"Profiler: {len(jumps)} valide Spruenge analysiert.",
    }


def _gold_scoring_mode(gold_standard_path):
    """Laedt den globalen Goldstandard als Score-Referenz (identisch fuer beide Phasen)."""
    try:
        gold = pd.read_excel(gold_standard_path).set_index("Feature")
        reference, deviation, raw_imp = {}, {}, {}
        for v in SCORE_VAR_NAMES:
            if v in gold.index:
                reference[v] = float(gold.loc[v, "GoldMean"])
                deviation[v] = float(gold.loc[v, "GoldStd"])
                raw_imp[v] = float(gold.loc[v, "Importance"])
            else:
                reference[v], deviation[v], raw_imp[v] = 0.0, 1.0, 0.0
        importance = normalize_importance(raw_imp, feature_names=SCORE_VAR_NAMES, target_sum=1.0)
    except Exception:
        reference = {v: 0.0 for v in SCORE_VAR_NAMES}
        deviation = {v: 1.0 for v in SCORE_VAR_NAMES}
        raw_imp = {v: 1.0 for v in SCORE_VAR_NAMES}
        importance = normalize_importance(raw_imp, feature_names=SCORE_VAR_NAMES, target_sum=1.0)
    return {"reference": reference, "deviation": deviation, "importance": importance}, 4.5


def load_scoring_profile(athlet_name, gold_standard_path, n_existing_jumps,
                         threshold=GOLD_TO_BASELINE_THRESHOLD):
    """Waehlt die zwei Referenz-Modi ("aufbau"/"halten") fuer die Score-Berechnung.

    Entscheidung (analog zum BaselineManager):
      * <name>_baseline.csv existiert UND es liegen mehr als `threshold` Spruenge
        in der alten <name>.csv  -> individuelle Baseline (Median/MAD/Importance)
      * sonst                                                      -> globaler Goldstandard

    Alte Baseline-CSVs ohne "Mode"-Spalte werden als "halten" interpretiert; der
    Modus "aufbau" weicht in diesem Fall auf den Goldstandard aus.

    Rueckgabe: (profiles, h_max, modus_text)
      profiles: {"aufbau": {"reference","deviation","importance"}, "halten": {...}}
      importance ist je Modus auf Summe = 1.0 normiert.
    """
    baseline_path = os.path.join("athleten_daten", f"{athlet_name}_baseline.csv")

    use_individual = (
        athlet_name not in ("master_session_daten", "global", "Profi-Standard (Master)")
        and os.path.exists(baseline_path)
        and n_existing_jumps > threshold
    )

    profiles, mode_sources, h_max = {}, {}, None

    if use_individual:
        try:
            df_base = pd.read_csv(baseline_path)
            if "Mode" not in df_base.columns:
                df_base = df_base.copy()
                df_base["Mode"] = "halten"

            for mode in ("aufbau", "halten"):
                df_mode = df_base[df_base["Mode"] == mode]
                if df_mode.empty:
                    continue
                df_mode = df_mode.set_index("Feature").reindex(SCORE_VAR_NAMES)
                if df_mode["Median"].notna().any():
                    reference = {v: (float(df_mode.loc[v, "Median"]) if pd.notna(df_mode.loc[v, "Median"]) else 0.0)
                                 for v in SCORE_VAR_NAMES}
                    deviation = {v: (float(df_mode.loc[v, "MAD"]) if pd.notna(df_mode.loc[v, "MAD"]) else 1.0)
                                 for v in SCORE_VAR_NAMES}
                    raw_imp = {v: (float(df_mode.loc[v, "Importance"]) if pd.notna(df_mode.loc[v, "Importance"]) else 0.0)
                               for v in SCORE_VAR_NAMES}
                    importance = normalize_importance(raw_imp, feature_names=SCORE_VAR_NAMES, target_sum=1.0)
                    profiles[mode] = {"reference": reference, "deviation": deviation, "importance": importance}
                    mode_sources[mode] = "individuelle Baseline"
                    h_max_vals = df_mode["H_Max"].dropna()
                    if len(h_max_vals) > 0 and h_max is None:
                        h_max = float(h_max_vals.iloc[0])
        except Exception:
            profiles, mode_sources = {}, {}

    if "aufbau" not in profiles or "halten" not in profiles:
        gold_profile, gold_h_max = _gold_scoring_mode(gold_standard_path)
        for mode in ("aufbau", "halten"):
            if mode not in profiles:
                profiles[mode] = gold_profile
                mode_sources[mode] = "globaler Goldstandard"
        if h_max is None:
            h_max = gold_h_max

    modus = f"aufbau: {mode_sources.get('aufbau', '?')}, halten: {mode_sources.get('halten', '?')}"
    return profiles, h_max, modus


def scores_for_jump(jump, profile):
    """Berechnet Trend- und Absolut-Score fuer einen Sprung-Dict gegen einen Modus-Profil-Dict.

    Verwendet exakt dieselbe Logik wie der JumpAnalyzer (compute_jump_score):
      * trend_score : mit Richtung (+ = "frueher treten")
      * abs_score   : reine Abweichung (gewichtetes Mittel der |z|)
    Liefert immer endliche floats. Rueckgabe: (trend_score, abs_score)
    """
    current = {f: jump.get(f, np.nan) for f in SCORE_FEATURES}
    res = compute_jump_score(
        current_features=current,
        reference=profile["reference"],
        deviation=profile["deviation"],
        importance=profile["importance"],
        direction=DIRECTION_MULTIPLIER,
        feature_order=SCORE_FEATURES,
    )
    return round(float(res["trend_score"]), 4), round(float(res["abs_score"]), 4)


def _append_with_schema(path, df_new, columns):
    """Haengt df_new an eine CSV an und stellt das Spaltenschema sicher (inkl. Migration alter Dateien)."""
    df_new = df_new.reindex(columns=columns)
    if not os.path.exists(path):
        df_new.to_csv(path, index=False, header=True)
        return
    try:
        existing = pd.read_csv(path)
    except Exception:
        df_new.to_csv(path, index=False, header=True)
        return

    if list(existing.columns) == list(columns):
        df_new.to_csv(path, mode="a", index=False, header=False)
    else:
        for c in columns:
            if c not in existing.columns:
                existing[c] = np.nan
        existing = existing.reindex(columns=columns)
        combined = pd.concat([existing, df_new], ignore_index=True)
        combined.to_csv(path, index=False, header=True)


def run_offline_profiler(raw_signal, athlet_name, fs, peak_height, peak_distance, a, b,
                         session_id=None, timestamp=None, gold_standard_path="goldTableNeu.xlsx"):
    """Analysiert die Rohdaten eines Durchgangs und schreibt zwei CSV-Dateien (inkl. Score)."""
    try:
        result = analyze_raw_signal(raw_signal, fs, peak_height, peak_distance, a, b)
        if not result["ok"]:
            return result["message"]

        jumps = result["jumps"]
        if len(jumps) == 0:
            return "Profiler: Keine validen Spruenge gefunden. Nichts gespeichert."

        from datetime import datetime
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if session_id is None:
            session_id = timestamp

        os.makedirs("athleten_daten", exist_ok=True)
        base_path = os.path.join("athleten_daten", f"{athlet_name}.csv")
        all_path = os.path.join("athleten_daten", f"{athlet_name}_all.csv")

        # ---- 0. Anzahl bereits vorhandener (hochwertiger) Spruenge in der alten <name>.csv ----
        n_existing = 0
        if os.path.exists(base_path):
            try:
                n_existing = len(pd.read_csv(base_path))
            except Exception:
                n_existing = 0

        # ---- 1. Referenz-Modi ("aufbau"/"halten") fuer die Score-Berechnung waehlen ----
        profiles, h_max, score_mode = load_scoring_profile(
            athlet_name, gold_standard_path, n_existing)

        # ---- 2. Score pro Kontakt berechnen (Trend + Absolut), Modus per Phasen-Weiche ----
        for j in jumps:
            phase = determine_phase(j.get("h_entering"), h_max)
            j["trend_score"], j["abs_score"] = scores_for_jump(j, profiles[phase])

        # ---- 3. Vollstaendige _all.csv (alle validen Spruenge inkl. Score) ----
        rows_all = []
        for j in jumps:
            row = dict(j)
            row["session_id"] = session_id
            row["timestamp"] = timestamp
            rows_all.append(row)
        _append_with_schema(all_path, pd.DataFrame(rows_all), ALL_COLUMNS)

        # ---- 4. Baseline-CSV: nur hochwertige Spruenge (HG > Schwelle), inkl. Score ----
        good_rows = [j for j in jumps if np.isfinite(j["HG"]) and j["HG"] > HG_QUALITY_THRESHOLD]
        good_count = len(good_rows)
        if good_count > 0:
            _append_with_schema(base_path, pd.DataFrame(good_rows), BASELINE_COLUMNS)

        return (f"Profiler: {len(jumps)} valide Spruenge in '{all_path}' gesichert "
                f"({good_count} davon hochwertig (HG>{HG_QUALITY_THRESHOLD}m) fuer die Baseline). "
                f"Score-Referenz: {score_mode} (alte <name>.csv: {n_existing} Spruenge).")

    except Exception as e:
        return f"Fehler im Offline-Profiler-Modul: {str(e)}"
