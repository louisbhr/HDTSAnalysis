# baseline_manager.py
import os
import numpy as np
import pandas as pd

from importance_utils import normalize_importance, MAD_CONSISTENCY
from profiler import HG_QUALITY_THRESHOLD

# Score-relevante Features (identisch zu profiler.SCORE_VAR_NAMES / jump_analyzer.var_names).
VAR_NAMES = ["Peak_t", "Peak_Prct", "Explosiv", "preSlope", "postSlope", "Symmetry"]

# Mindestanzahl Spruenge pro Modus. Unterschreitung:
#   * "halten": Fallback auf den Goldstandard (Median/MAD/Importance)
#   * "aufbau": KEIN Fallback - es werden keine Aufbau-Zeilen gespeichert.
#     Der Goldstandard beschreibt Steady-State-Kontakte und waere als
#     Aufbau-Referenz genau falsch; die Ampel faehrt dann im Aufbau nur das
#     diffI-Kriterium (siehe esp_client.classify_ampel), Richtungslichter
#     erst, sobald die Aufbau-Baseline steht.
MIN_JUMPS_PER_MODE = 15

# H_Max_robust = dieses Perzentil der Height-Spalte (statt max(), damit ein einzelner
# Ausreisser-Flug die Phasengrenze nicht verschiebt).
H_MAX_PERCENTILE = 95

# Modus "halten": alle Spruenge mit Height >= diesem Anteil von H_Max_robust.
HALTEN_HEIGHT_FRACTION = 0.9


def _hybrid_importance(df_mode, var_names, mode_n, old_importance, old_n, gold_standard_path):
    """Hybrid-Feature-Importance fuer einen Modus.

    < 100 Spruenge im Modus  -> Goldstandard-Importance uebernehmen.
    >= 100 Spruenge im Modus -> Regression (|Korrelation| zu HG) als Roh-Importance,
                                 mit Intervall-Sperre (nur bei neuer Hundertergrenze
                                 neu rechnen), analog zur bisherigen Logik.
    """
    if mode_n < 100:
        try:
            gold = pd.read_excel(gold_standard_path).set_index("Feature")
            raw_importance = {var: float(gold.loc[var, "Importance"]) if var in gold.index else 0.0
                               for var in var_names}
        except Exception:
            raw_importance = {var: 1.0 for var in var_names}
        return raw_importance, "Goldstandard-Modus"

    recalc_importance = True
    if old_importance and (mode_n // 100) == (old_n // 100) and len(old_importance) == len(var_names):
        recalc_importance = False

    raw_importance = {}
    if recalc_importance:
        try:
            df_clean = df_mode[var_names + ["HG"]].dropna()
            if len(df_clean) <= 15:
                recalc_importance = False
            else:
                for var in var_names:
                    corr = df_clean[var].corr(df_clean["HG"])
                    raw_importance[var] = abs(corr) if np.isfinite(corr) else 0.0
                if sum(raw_importance.values()) <= 0:
                    recalc_importance = False
        except Exception:
            recalc_importance = False

    if not recalc_importance:
        raw_importance = {var: old_importance.get(var, 1.0) for var in var_names}

    modus_text = ("Regressions-Modus (aktualisiert)" if recalc_importance else
                  "Regressions-Modus (gesperrt, naechster Hunderter-Schritt abwarten)")
    return raw_importance, modus_text


def _gold_fallback_row_values(gold_standard_path, var_names):
    """Median/MAD/Importance direkt vom Goldstandard uebernehmen (Modus-Fallback bei zu wenig Daten).

    GoldStd ist bereits eine Standardabweichung (Sigma-Skala) - hier NICHT nochmal
    mit MAD_CONSISTENCY multiplizieren, das gilt nur fuer echte (aus Rohdaten
    berechnete) MAD-Werte.
    """
    try:
        gold = pd.read_excel(gold_standard_path).set_index("Feature")
        medians = {v: float(gold.loc[v, "GoldMean"]) if v in gold.index else 0.0 for v in var_names}
        mads = {v: float(gold.loc[v, "GoldStd"]) if v in gold.index else 1.0 for v in var_names}
        raw_importance = {v: float(gold.loc[v, "Importance"]) if v in gold.index else 0.0 for v in var_names}
    except Exception:
        medians = {v: 0.0 for v in var_names}
        mads = {v: 1.0 for v in var_names}
        raw_importance = {v: 1.0 for v in var_names}
    return medians, mads, raw_importance


def update_athlete_baseline(athlet_name, gold_standard_path="goldTableNeu.xlsx"):
    """
    BaselineManager: Berechnet ZWEI Referenzsaetze ("aufbau"/"halten") aus der
    <name>_all.csv eines Athleten (Median, MAD, H_Max, HG_Avg, Importance je Modus).

    Hintergrund (Validierungsstudie): der alte Filter HG > 0.1 (auf die bereits
    gefilterte <name>.csv angewandt) selektierte systematisch "Lade-Spruenge" mit
    langem Kontakt - genau die Spruenge, die NICHT das Ziel-Timing fuers Hoehe-Halten
    zeigen (Score korreliert within-Person +0.51 mit HG und -0.71 mit Hoehe). Die
    besten Steady-State-Spruenge wuerden von einer einzelnen HG-Baseline als
    Abweichung geflaggt. Daher zwei getrennte Referenzsaetze:
      * "aufbau": alle Spruenge mit HG > HG_QUALITY_THRESHOLD (Hoehe wird aufgebaut)
      * "halten": alle Spruenge mit Height >= 0.9 * H_Max_robust (nahe Bestleistung)

    MAD wird nach der Berechnung mit MAD_CONSISTENCY (1.4826) multipliziert, damit
    sie ein robuster Sigma-Schaetzer ist und individuelle z-Werte auf derselben
    Skala liegen wie die Gold-z-Werte (Std-basiert).
    """
    if athlet_name in ["master_session_daten", "global", "Profi-Standard (Master)"]:
        return "Für den Master-Standard wird keine eigene Referenz berechnet."

    if not os.path.exists(gold_standard_path):
        return "Fehler: Goldstandard-Datei nicht gefunden."

    # ---- 1. Pfade definieren und Rohdaten pruefen (Quelle: <name>_all.csv, ALLE Spruenge) ----
    all_path = os.path.join("athleten_daten", f"{athlet_name}_all.csv")
    baseline_path = os.path.join("athleten_daten", f"{athlet_name}_baseline.csv")

    if not os.path.exists(all_path):
        return f"Noch keine Aufzeichnungen für '{athlet_name}' vorhanden."

    df_all = pd.read_csv(all_path)
    if len(df_all) == 0:
        return "Keine Sprünge aufgezeichnet – keine Referenz berechnet."

    # ---- 2. H_Max_robust: 95. Perzentil der Height-Spalte (robust ggue. Ausreissern) ----
    height_vals = df_all["Height"].dropna().values if "Height" in df_all.columns else np.array([])
    h_max_robust = float(np.percentile(height_vals, H_MAX_PERCENTILE)) if len(height_vals) > 0 else 4.5

    # ---- 3. Zwei Sprung-Teilmengen (Modi) aus der vollstaendigen Historie ----
    aufbau_df = (df_all[df_all["HG"] > HG_QUALITY_THRESHOLD] if "HG" in df_all.columns
                 else df_all.iloc[0:0])
    halten_df = (df_all[df_all["Height"] >= HALTEN_HEIGHT_FRACTION * h_max_robust] if "Height" in df_all.columns
                 else df_all.iloc[0:0])
    mode_dfs = {"aufbau": aufbau_df, "halten": halten_df}

    # ---- 4. Alte Baseline einlesen (fuer Importance-Hybrid), nach Modus gruppiert ----
    old_importance_by_mode = {"aufbau": {}, "halten": {}}
    old_n_by_mode = {"aufbau": 0, "halten": 0}
    if os.path.exists(baseline_path):
        try:
            df_old = pd.read_csv(baseline_path)
            if "Mode" not in df_old.columns:
                df_old = df_old.copy()
                df_old["Mode"] = "halten"
            for mode in ("aufbau", "halten"):
                df_old_mode = df_old[df_old["Mode"] == mode]
                if not df_old_mode.empty:
                    old_importance_by_mode[mode] = dict(zip(df_old_mode["Feature"], df_old_mode["Importance"]))
                    if "Total_Jumps" in df_old_mode.columns:
                        old_n_by_mode[mode] = int(df_old_mode["Total_Jumps"].iloc[0])
        except Exception:
            pass

    # ---- 5. Median/MAD/Importance je Modus berechnen (oder Goldstandard-Fallback) ----
    rows_to_save = []
    modus_texts = []

    for mode in ("aufbau", "halten"):
        df_mode = mode_dfs[mode]
        mode_n = len(df_mode)
        hg_avg = float(df_mode["HG"].mean()) if (mode_n > 0 and "HG" in df_mode.columns) else float("nan")

        if mode_n < MIN_JUMPS_PER_MODE:
            if mode == "aufbau":
                # Bewusst KEIN Goldstandard-Fallback: Steady-State-Werte waeren als
                # Aufbau-Referenz genau falsch. Ohne gespeicherte Aufbau-Zeilen
                # erkennt der Loader den Modus als fehlend -> Ampel nutzt im Aufbau
                # nur das diffI-Kriterium.
                modus_texts.append(f"Aufbau: nur {mode_n} Sprünge – kein Referenzsatz "
                                    f"(Ampel nutzt im Aufbau nur den Höhengewinn)")
                continue
            medians, mads, importances_raw = _gold_fallback_row_values(gold_standard_path, VAR_NAMES)
            modus_texts.append(f"{mode.capitalize()}: Standard-Referenz ({mode_n} Sprünge)")
        else:
            medians, mads = {}, {}
            for var in VAR_NAMES:
                vals = df_mode[var].dropna().values if var in df_mode.columns else np.array([])
                if len(vals) > 0:
                    med = float(np.median(vals))
                    mad_raw = float(np.median(np.abs(vals - med)))
                    mads[var] = (mad_raw * MAD_CONSISTENCY) if mad_raw > 0 else 1e-6
                    medians[var] = med
                else:
                    medians[var], mads[var] = 0.0, 1.0

            importances_raw, _imp_text = _hybrid_importance(
                df_mode, VAR_NAMES, mode_n, old_importance_by_mode[mode], old_n_by_mode[mode],
                gold_standard_path)
            modus_texts.append(f"{mode.capitalize()}: {mode_n} Sprünge")

        importances = normalize_importance(importances_raw, feature_names=VAR_NAMES, target_sum=1.0)

        for var in VAR_NAMES:
            rows_to_save.append({
                "Feature": var,
                "Median": medians[var],
                "MAD": mads[var],
                "Importance": importances[var],
                "H_Max": h_max_robust,
                "HG_Avg": hg_avg,
                "Total_Jumps": mode_n,
                "Mode": mode,
            })

    # ---- 6. Speichern ----
    os.makedirs("athleten_daten", exist_ok=True)
    pd.DataFrame(rows_to_save).to_csv(baseline_path, index=False)

    return (f"Referenz aktualisiert – Besthöhe {h_max_robust:.2f} m; "
            + "; ".join(modus_texts) + ".")
