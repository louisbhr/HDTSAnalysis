# baseline_manager.py
import os
import numpy as np
import pandas as pd

from importance_utils import normalize_importance


def update_athlete_baseline(athlet_name, gold_standard_path="goldTableNeu.xlsx"):
    """
    BaselineManager: Berechnet die Vergleichswerte (Median, MAD, H_Max, HG_Avg) und die
    Feature Importance (Hybrid/Regressiv) fuer einen Athleten nach Trainingsende.

    NEU: Die Feature-Importance wird in JEDEM Modus zentral ueber importance_utils
    auf Summe = 1.0 normalisiert. Damit liegen alle Modi auf derselben Skala und der
    Score (in jump_analyzer) ist nicht mehr ueberhoeht.
    """
    if athlet_name in ["master_session_daten", "global", "Profi-Standard (Master)"]:
        return "Baseline-Manager: Fuer den Master-Standard wird keine lokale Baseline berechnet."

    if not os.path.exists(gold_standard_path):
        return f"Baseline-Manager Fehler: Die Goldstandard-Datei '{gold_standard_path}' wurde nicht gefunden!"

    # ---- 1. Pfade definieren und Rohdaten pruefen ----
    filepath = os.path.join("athleten_daten", f"{athlet_name}.csv")
    baseline_path = os.path.join("athleten_daten", f"{athlet_name}_baseline.csv")

    if not os.path.exists(filepath):
        return f"Baseline-Manager: Keine Rohdaten-Historie fuer '{athlet_name}' gefunden."

    df = pd.read_csv(filepath)
    n_jumps = len(df)

    if n_jumps <= 50:
        return f"Baseline-Manager: Mit {n_jumps} Spruengen noch nicht genuegend Daten vorhanden (>50 benoetigt)."

    var_names = ["Peak", "Peak_t", "Peak_Prct", "timing", "Explosiv",
    "preSlope", "postSlope", "Symmetry"]

    # ---- 2. Median und MAD ----
    medians, mads = {}, {}
    for var in var_names:
        if var in df.columns:
            vals = df[var].dropna().values
            if len(vals) > 0:
                med = np.median(vals)
                mad = np.median(np.abs(vals - med))
                if mad == 0:
                    mad = 1e-6
                medians[var] = med
                mads[var] = mad
            else:
                medians[var], mads[var] = 0.0, 1.0
        else:
            medians[var], mads[var] = 0.0, 1.0

    # ---- 3. Max-Hoehe und durchschnittlicher Hoehengewinn ----
    h_max = df["Height"].max() if "Height" in df.columns else 0.0
    hg_avg = df["HG"].mean() if "HG" in df.columns else 0.0

    # ---- 4. Feature Importance (Hybrid) ----
    recalc_importance = True
    old_importance = {}
    old_jumps = 0

    if os.path.exists(baseline_path):
        try:
            df_old = pd.read_csv(baseline_path)
            old_importance = dict(zip(df_old["Feature"], df_old["Importance"]))
            old_jumps = int(df_old["Total_Jumps"].iloc[0]) if "Total_Jumps" in df_old.columns else 0

            # Intervall-Sperre: nur bei neuer Hundertergrenze neu rechnen.
            if n_jumps >= 100:
                if (n_jumps // 100) == (old_jumps // 100) and len(old_importance) == len(var_names):
                    recalc_importance = False
            else:
                recalc_importance = False  # < 100 Spruenge: starr Goldstandard
        except Exception:
            pass

    raw_importance = {}

    # 4A < 100 Spruenge -> Goldstandard-Importance uebernehmen
    if n_jumps < 100:
        try:
            gold = pd.read_excel(gold_standard_path).set_index("Feature")
            for var in var_names:
                raw_importance[var] = float(gold.loc[var, "Importance"]) if var in gold.index else 0.0
        except Exception:
            raw_importance = {var: 1.0 for var in var_names}

    # 4B >= 100 Spruenge -> Regression (|Korrelation| zu HG) als Roh-Importance
    else:
        if recalc_importance:
            try:
                df_clean = df[var_names + ["HG"]].dropna()
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

        # Fallback: alte (bereits auf 1.0 normierte) Importances weiterverwenden.
        if not recalc_importance:
            raw_importance = {var: old_importance.get(var, 1.0) for var in var_names}

    # ---- 4c. ZENTRALE Normalisierung: Summe = 1.0 (eine einzige Stelle) ----
    importances = normalize_importance(raw_importance, feature_names=var_names, target_sum=1.0)

    # ---- 5. Speichern ----
    rows_to_save = [{
        "Feature": var,
        "Median": medians[var],
        "MAD": mads[var],
        "Importance": importances[var],
        "H_Max": h_max,
        "HG_Avg": hg_avg,
        "Total_Jumps": n_jumps,
    } for var in var_names]

    os.makedirs("athleten_daten", exist_ok=True)
    pd.DataFrame(rows_to_save).to_csv(baseline_path, index=False)

    modus_text = "Regressions-Modus (aktualisiert)" if recalc_importance else \
                 "Regressions-Modus (gesperrt, naechster Hunderter-Schritt abwarten)"
    if n_jumps < 100:
        modus_text = "Goldstandard-Modus"

    return (f"Baseline-Manager: '{baseline_path}' erfolgreich berechnet "
            f"({n_jumps} Spruenge im {modus_text}, Importance-Summe = 1.0).")
