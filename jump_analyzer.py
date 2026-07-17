import os
import numpy as np
import pandas as pd
from scipy.signal import butter, lfilter, lfilter_zi, find_peaks
from scipy.integrate import trapezoid

from importance_utils import (
    normalize_importance, compute_jump_score, step_label, format_debug_table,
)


class JumpAnalyzer:
    """
    JumpAnalyzer: Verarbeitet die Live-Daten aus Qira, erkennt Spruenge, berechnet Features
    und gibt Coaching-Tipps basierend auf einem individuellen oder globalen Goldstandard.

    NEU: Feature-Importance wird zentral ueber importance_utils auf Summe = 1.0 normiert.
    Der Score ist damit ein gewichtetes Mittel der absoluten Abweichungen (in Std/MAD)
    und nicht mehr um Faktor ~4.5 ueberhoeht. Zusaetzlich werden Trend- und Absolut-Score
    getrennt berechnet und pro Sprung als Debug-Tabelle geloggt.
    """

    # ---- 1. Initialisierung ----
    def __init__(self):
        self.storage = []
        self.pks_storage = []
        self.idx_storage = []
        self.left_idx = []
        self.right_idx = []

        self.last_block_id = -1
        self.n_jumps = 0
        self.total_jump_count = 0
        self.last_analyzed_jump_idx = -1

        # Feste Parameter
        self.f_order = 2
        self.f_frequency = 12
        self.peak_height = 2200
        self.peak_distance = 1000
        self.fs_file = 1000
        self.window = int(0.25 * self.fs_file)
        self.g = 9.81

        # Dynamische Athleten-Parameter
        self.h_max = 4.5
        self.var_names = ["Peak", "Peak_t", "Peak_Prct", "timing", "Explosiv",
        "preSlope", "postSlope", "Symmetry"]

        self.gold_standard_use = None
        self.gold_dev_use = None
        self.importance = None
        self.gold_features_use = np.array(self.var_names)

        # Dict-Repraesentationen fuer die zentrale Score-Berechnung.
        self.reference = {}
        self.deviation = {}
        self.importance_dict = {}

        # Korrekturrichtung je Feature (+1: hoeher ist besser, -1: niedriger ist besser).
        self.direction_multiplier = {
            "Peak_t": 1, "timing": 1, "Peak_Prct": 1,
            "Symmetry": 1, "postSlope": 1, "preSlope": -1, "Explosiv": -1,
        }

        self.data = {var: [] for var in self.var_names}
        self.data["coaching"] = []

        # Filter initialisieren
        self.b, self.a = butter(self.f_order, self.f_frequency / (self.fs_file / 2), btype='low')
        self.zi = lfilter_zi(self.b, self.a)

        self.load_profile("global")

    # ---- 1b. Dict-Repraesentationen aus den Arrays aufbauen ----
    def _build_lookup_dicts(self):
        """Baut reference/deviation/importance_dict aus den geladenen Arrays.

        Die Importance wird hier zentral auf Summe = 1.0 normiert (eine einzige Stelle).
        """
        self.reference = {var: float(self.gold_standard_use[i]) for i, var in enumerate(self.var_names)}
        self.deviation = {var: float(self.gold_dev_use[i]) for i, var in enumerate(self.var_names)}

        raw_imp = {var: float(self.importance[i]) for i, var in enumerate(self.var_names)}
        self.importance_dict = normalize_importance(raw_imp, feature_names=self.var_names, target_sum=1.0)
        # importance-Array konsistent auf 1.0-Skala zuruecksetzen.
        self.importance = np.array([self.importance_dict[var] for var in self.var_names])

    # ---- 2. Profil laden ----
    def load_profile(self, athlet_name, logFcn=print):
        """Laedt das individuelle Baseline-Profil oder weicht auf den Goldstandard aus.

        In ALLEN Faellen wird die Importance am Ende zentral auf Summe = 1.0 normiert.
        """
        baseline_path = os.path.join("athleten_daten", f"{athlet_name}_baseline.csv")

        if athlet_name not in ["master_session_daten", "global"] and os.path.exists(baseline_path):
            try:
                df_base = pd.read_csv(baseline_path).set_index("Feature")
                df_ordered = df_base.reindex(self.var_names)

                self.gold_standard_use = df_ordered["Median"].values
                self.gold_dev_use = df_ordered["MAD"].values
                self.importance = df_ordered["Importance"].values
                self.h_max = float(df_ordered["H_Max"].iloc[0])

                self._build_lookup_dicts()
                logFcn(f"JumpAnalyzer: Individuelle Baseline fuer '{athlet_name}' geladen "
                f"(H_Max: {self.h_max:.2f}m, Importance-Summe normiert auf 1.0).")
                return
            except Exception as e:
                logFcn(f"JumpAnalyzer: Fehler beim Laden der Baseline von {athlet_name}. "
                f"Weiche auf Goldstandard aus. Fehler: {e}")

        # Fallback: globaler Goldstandard
        self.h_max = 4.5
        try:
            gold = pd.read_excel("goldTableNeu.xlsx").set_index("Feature")
            gold_ordered = gold.reindex(self.var_names)

            self.gold_standard_use = gold_ordered["GoldMean"].values
            self.gold_dev_use = gold_ordered["GoldStd"].values
            self.importance = gold_ordered["Importance"].values

            self._build_lookup_dicts()
            logFcn("JumpAnalyzer: Globalen Profi-Goldstandard geladen (Importance-Summe normiert auf 1.0).")
        except Exception as e:
            # Hardcoded Not-Fallback (Reihenfolge identisch zu self.var_names)
            self.gold_standard_use = np.array([3000, 0.2, 50, 10, 15000, 50000, -50000, 1.0], dtype=float)
            self.gold_dev_use = np.array([300, 0.02, 5, 1.5, 2000, 8000, 8000, 0.1], dtype=float)
            self.importance = np.full(8, 1.0, dtype=float)
            self._build_lookup_dicts()
            logFcn(f"JumpAnalyzer: KRITISCHER FEHLER beim Excel-Laden. Lokaler Not-Fallback aktiv! {e}")

    # ---- 3. Kontaktgrenzen fuer einen Peak ----
    def _calculate_contact_bounds_for_peak(self, i, signal_array):
        idx = self.idx_storage[i]
        pks = self.pks_storage[i]
        threshold = 0.05 * pks

        left_bound = round((self.idx_storage[i - 1] + idx) / 2) if i > 0 else max(0, idx - self.window)
        segL = signal_array[left_bound:idx]
        under_thresh_L = np.where(segL < threshold)[0]
        x_l = left_bound + under_thresh_L[-1] if len(under_thresh_L) > 0 else left_bound

        if i < len(self.idx_storage) - 1:
            right_bound = round((idx + self.idx_storage[i + 1]) / 2)
        else:
            right_bound = min(len(signal_array), idx + self.window)

        segR = signal_array[idx:right_bound]
        under_thresh_R = np.where(segR < threshold)[0]
        x_r = idx + under_thresh_R[0] if len(under_thresh_R) > 0 else right_bound

        try:
            if 0 < x_l < len(signal_array):
                y2, y1 = signal_array[x_l], signal_array[x_l - 1]
                x_interp_l = x_l - 1 + (threshold - y1) / (y2 - y1) if y2 != y1 else x_l
            else:
                x_interp_l = float(x_l)

            if 0 < x_r < len(signal_array):
                y2, y1 = signal_array[x_r], signal_array[x_r - 1]
                x_interp_r = x_r - 1 + (threshold - y1) / (y2 - y1) if y2 != y1 else x_r
            else:
                x_interp_r = float(x_r)
        except Exception:
            x_interp_l, x_interp_r = float(x_l), float(x_r)

        if not np.isnan(x_interp_l) and not np.isnan(x_interp_r) and x_interp_r > x_interp_l:
            return x_interp_l, x_interp_r
        return None, None

    # ---- 4. Hauptfunktion: Verarbeitung der Daten aus Qira ----
    def process(self, d, block_id, logFcn=print):
        # ~ 4.1 Aufnahme neuer Daten ~
        if self.last_block_id == block_id:
            return
        self.last_block_id = block_id

        d = np.asarray(d).flatten()
        filt_sig, self.zi = lfilter(self.b, self.a, d, zi=self.zi)
        old_len = len(self.storage)
        self.storage.extend(filt_sig)

        # self.storage auf max. 5000 Werte begrenzen
        if len(self.storage) > 5000:
            diff = len(self.storage) - 5000
            self.storage = self.storage[diff:]

            while len(self.idx_storage) > 0 and (self.idx_storage[0] - diff) <= 0:
                self.idx_storage.pop(0)
                self.pks_storage.pop(0)
                if len(self.left_idx) > 0:
                    self.left_idx.pop(0)
                if len(self.right_idx) > 0:
                    self.right_idx.pop(0)
                self.last_analyzed_jump_idx -= 1

            self.idx_storage = [i - diff for i in self.idx_storage]
            self.left_idx = [(i - diff) if i is not None else None for i in self.left_idx]
            self.right_idx = [(i - diff) if i is not None else None for i in self.right_idx]

        signal_array = np.array(self.storage)

        # ~ 4.2 Peak-Suche ~
        lookback = 350
        search_start = max(0, old_len - lookback)
        search_segment = signal_array[search_start:]
        peaks, props = find_peaks(search_segment, height=self.peak_height)

        if len(peaks) > 0:
            actual_idx = peaks + search_start
            actual_pks = props["peak_heights"]
            for current_idx, current_pk in zip(actual_idx, actual_pks):
                if len(self.idx_storage) == 0:
                    self.idx_storage.append(current_idx)
                    self.pks_storage.append(current_pk)
                else:
                    dist = current_idx - self.idx_storage[-1]
                    if dist >= self.peak_distance:
                        self.idx_storage.append(current_idx)
                        self.pks_storage.append(current_pk)
                    elif current_pk > self.pks_storage[-1]:
                        self.idx_storage[-1] = current_idx
                        self.pks_storage[-1] = current_pk

        while len(self.left_idx) < len(self.idx_storage):
            i = len(self.left_idx)
            x_l, x_r = self._calculate_contact_bounds_for_peak(i, signal_array)
            self.left_idx.append(x_l)
            self.right_idx.append(x_r)

        self.n_jumps = len(self.idx_storage)

        # ~ 4.3 Feature-Berechnung & Coaching ~
        while self.last_analyzed_jump_idx < len(self.idx_storage) - 1:
            next_jump_idx = self.last_analyzed_jump_idx + 1

            if self.left_idx[next_jump_idx] is None or self.right_idx[next_jump_idx] is None:
                self.last_analyzed_jump_idx = next_jump_idx
                continue

            left = int(round(self.left_idx[next_jump_idx]))
            right = int(round(self.right_idx[next_jump_idx]))
            peak = self.pks_storage[next_jump_idx]
            peak_idx = self.idx_storage[next_jump_idx]

            if left < 0 or right > len(signal_array) or right <= left:
                self.last_analyzed_jump_idx = next_jump_idx
                continue

            jump = signal_array[left:right]
            if len(jump) < 2:
                self.last_analyzed_jump_idx = next_jump_idx
                continue

            idx_peak = peak_idx - left
            if idx_peak <= 1 or idx_peak >= len(jump):
                self.last_analyzed_jump_idx = next_jump_idx
                continue

            contact_t = len(jump) / self.fs_file
            t_seg = np.arange(left, right) / self.fs_file
            peak_t = (peak_idx / self.fs_file) - (left / self.fs_file)

            if contact_t > 0:
                peak_prct = 100 * peak_t / contact_t
                timing = peak_prct * contact_t
            else:
                peak_prct, timing = np.nan, np.nan

            explosiv = peak / peak_t if peak_t > 0 else np.nan

            F_pre = jump[:idx_peak]
            t_pre = t_seg[:idx_peak]
            t_pre_norm = t_pre - t_pre[0]
            pre_slope = np.polyfit(t_pre_norm, F_pre, 1)[0] if len(F_pre) >= 2 and np.ptp(t_pre_norm) > 0 else np.nan

            F_post = jump[idx_peak:]
            t_post = t_seg[idx_peak:]
            t_post_norm = t_post - t_post[0]
            post_slope = np.polyfit(t_post_norm, F_post, 1)[0] if len(F_post) >= 2 and np.ptp(t_post_norm) > 0 else np.nan

            pre_integral = trapezoid(F_pre, t_pre)
            post_integral = trapezoid(F_post, t_post)
            sym = np.nan if abs(post_integral) < 1e-4 else pre_integral / post_integral

            self.data["Peak"].append(peak)
            self.data["Peak_t"].append(peak_t)
            self.data["Peak_Prct"].append(peak_prct)
            self.data["timing"].append(timing)
            self.data["Explosiv"].append(explosiv)
            self.data["preSlope"].append(pre_slope)
            self.data["postSlope"].append(post_slope)
            self.data["Symmetry"].append(sym)

            # ~ 4.4 Coaching-Logik ~
            z_threshold_limit = 1.5

            # High-Performance-Zone: Toleranz erweitern, wenn vorheriger Flug nahe Bestleistung war.
            if next_jump_idx > 0 and self.right_idx[next_jump_idx - 1] is not None:
                letztes_kontakt_ende = self.right_idx[next_jump_idx - 1]
                if left > letztes_kontakt_ende:
                    t_flug = (left - letztes_kontakt_ende) / self.fs_file
                    h_previous = 0.125 * self.g * (t_flug ** 2)
                    if self.h_max > 0:
                        prozent_von_max = (h_previous / self.h_max) * 100
                        if prozent_von_max > 90.0:
                            z_threshold_limit = 2.5
                            logFcn(f"High-Performance-Zone detektiert "
                            f"({prozent_von_max:.1f}% von Max-Hoehe, h={h_previous:.2f}m).")

            current_features = {
                "Peak_t": peak_t, "Peak_Prct": peak_prct, "timing": timing,
                "Explosiv": explosiv,
                "preSlope": pre_slope, "postSlope": post_slope, "Symmetry": sym,
            }

            # ZENTRALE Score-Berechnung (Importance intern auf Summe = 1.0 normiert).
            result = compute_jump_score(
                current_features=current_features,
                reference=self.reference,
                deviation=self.deviation,
                importance=self.importance_dict,
                direction=self.direction_multiplier,
                feature_order=self.var_names,
            )

            trend_score = result["trend_score"]   # mit Richtung (+ = "frueher treten")
            abs_score = result["abs_score"]        # reine Abweichung, gewichtetes Mittel der |z|
            max_abs_delta = result["max_abs_delta"]

            step = step_label(abs_score)
            direction = "frueher treten" if trend_score > 0 else "spaeter treten"
            coaching_output = f"Athlet muss {step} {direction}"

            self.total_jump_count += 1
            self.data["coaching"].append(coaching_output)

            if abs_score > z_threshold_limit:
                debug_table = format_debug_table(result["details"])
                logFcn(
                    f"Sprung #{self.total_jump_count} erkannt! Coaching: {coaching_output}\n"
                    f"   Trend-Score (mit Richtung): {trend_score:+.3f} | "
                    f"Absolut-Score (Abweichung): {abs_score:.3f} | "
                    f"max |z|: {max_abs_delta:.2f}\n{debug_table}"
                )
            else:
                logFcn(f"Sprung #{self.total_jump_count} erkannt! Alle Werte im gruenen Bereich. "
                f"Gut gemacht! (Absolut-Score: {abs_score:.3f})")

            self.last_analyzed_jump_idx = next_jump_idx

    def reset(self):
        self.storage = []
        self.pks_storage = []
        self.idx_storage = []
        self.left_idx = []
        self.right_idx = []
        self.last_block_id = -1
        self.n_jumps = 0
        self.total_jump_count = 0
        self.last_analyzed_jump_idx = -1
        self.zi = lfilter_zi(self.b, self.a)
        self.data = {var: [] for var in self.var_names}
        self.data["coaching"] = []
