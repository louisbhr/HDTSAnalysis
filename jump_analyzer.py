import os
import numpy as np
import pandas as pd
from scipy.signal import butter, lfilter, lfilter_zi, find_peaks
from scipy.integrate import trapezoid

from importance_utils import (
    normalize_importance, compute_jump_score, step_label,
    DEADBAND_TREND, CONSISTENCY_GATE, determine_phase,
)

# Ampel-Zuordnung (ESP32): optional - der Analyzer laeuft auch ohne esp_client.
try:
    from esp_client import classify_ampel
except Exception:
    classify_ampel = None

# Schwelle fuer die Output-orientierte Ampel in Phase "aufbau": diffI (= Integral(i)
# - Integral(i-1)) ist am Kontaktende latenzfrei verfuegbar und praediziert den erst
# beim naechsten Kontakt messbaren Hoehengewinn HG.
AUFBAU_DIFFI_THRESHOLD = 0.0


class JumpAnalyzer:
    """
    JumpAnalyzer: Verarbeitet die Live-Daten aus Qira, erkennt Spruenge, berechnet Features
    und gibt Coaching-Tipps basierend auf einem individuellen oder globalen Goldstandard.

    Feature-Importance wird zentral ueber importance_utils auf Summe = 1.0 normiert.
    Der Score ist damit ein gewichtetes Mittel der absoluten Abweichungen (in Std/MAD).

    NEU: Es werden ZWEI Referenzsaetze ("aufbau"/"halten") pro Athlet geladen. Welcher
    Modus fuer den aktuellen Kontakt gilt, wird ueber die Flughoehe VOR diesem Kontakt
    relativ zur Max-Hoehe bestimmt (siehe importance_utils.determine_phase). Die
    Coaching-Ausgabe ist entsprechend phasenabhaengig: in "halten" wird das Timing
    gegen die Referenz bewertet (mit Totband + Konsistenz-Gate), in "aufbau" gibt es
    stattdessen eine output-orientierte Rueckmeldung anhand von diffI.
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

        # Score-relevante Features. "Peak" wird nie in current_features uebergeben
        # (totes Feature) und "timing" ist exakt 100*Peak_t (perfekte Kollinearitaet
        # mit Peak_Prct, doppeltes Gewicht) - beide raus aus dem Score (Validierungsstudie).
        self.var_names = ["Peak_t", "Peak_Prct", "Explosiv",
        "preSlope", "postSlope", "Symmetry"]

        # Alle live berechneten Groessen fuer self.data (inkl. Peak/timing, die weiterhin
        # berechnet werden, sowie die neuen Ampel-Praediktoren Contact_t/Integral/diffI).
        self.log_var_names = ["Peak", "Peak_t", "Peak_Prct", "timing", "Explosiv",
        "preSlope", "postSlope", "Symmetry", "Contact_t", "Integral", "diffI"]

        # Zwei Referenzsaetze (Modi). Struktur je Modus:
        #   {"reference": {...}, "deviation": {...}, "importance_dict": {...}}
        self.profiles = {}
        # Herkunft je Modus ("individuelle Baseline" | "Goldstandard") - steuert
        # u.a., ob die Ampel im Aufbau Richtungslichter zeigen darf.
        self.mode_sources = {}

        # Optionale ESP32-Ampel: wird von der GUI per set_ampel_client() gesetzt.
        self.ampel_client = None
        self.last_ampel_state = ("OFF", 0)

        # Optionaler Per-Sprung-Callback fuer ein Live-Dashboard (GUI). Wird per
        # set_on_jump() gesetzt und nach jedem erkannten Sprung mit einem kompakten
        # dict aufgerufen. Ohne Callback laeuft die Analyse unveraendert weiter.
        self.on_jump = None

        # Integral des zuletzt verarbeiteten Kontakts, fuer diffI = Integral(i) - Integral(i-1).
        self.last_integral = None

        # Korrekturrichtung je Feature (+1: hoeher ist besser, -1: niedriger ist besser).
        self.direction_multiplier = {
            "Peak_t": 1, "Peak_Prct": 1,
            "Symmetry": 1, "postSlope": 1, "preSlope": -1, "Explosiv": -1,
        }

        self.data = {var: [] for var in self.log_var_names}
        self.data["coaching"] = []

        # Filter initialisieren
        self.b, self.a = butter(self.f_order, self.f_frequency / (self.fs_file / 2), btype='low')
        self.zi = lfilter_zi(self.b, self.a)

        self.load_profile("global")

    # ---- 1b. Ein einzelnes Referenz-Set (Modus) aus Median/MAD/Importance-Series bauen ----
    def _build_mode_dict(self, medians, mads, importances):
        """Baut ein {"reference","deviation","importance_dict"}-Dict fuer einen Modus.

        Die Importance wird hier zentral auf Summe = 1.0 normiert (eine einzige Stelle).
        """
        reference = {var: float(medians[var]) for var in self.var_names}
        deviation = {var: float(mads[var]) for var in self.var_names}
        raw_imp = {var: float(importances[var]) for var in self.var_names}
        importance_dict = normalize_importance(raw_imp, feature_names=self.var_names, target_sum=1.0)
        return {"reference": reference, "deviation": deviation, "importance_dict": importance_dict}

    def _gold_mode(self, logFcn=print):
        """Laedt den globalen Goldstandard als Fallback-Modus (identisch fuer beide Phasen)."""
        try:
            gold = pd.read_excel("goldTableNeu.xlsx").set_index("Feature")
            gold_ordered = gold.reindex(self.var_names)
            mode = self._build_mode_dict(
                gold_ordered["GoldMean"], gold_ordered["GoldStd"], gold_ordered["Importance"])
            return mode, 4.5
        except Exception as e:
            logFcn(f"Fehler beim Laden des Goldstandards – Not-Referenz aktiv ({e}).")
            medians = {"Peak_t": 0.2, "Peak_Prct": 50, "Explosiv": 15000,
                       "preSlope": 50000, "postSlope": -50000, "Symmetry": 1.0}
            mads = {"Peak_t": 0.02, "Peak_Prct": 5, "Explosiv": 2000,
                    "preSlope": 8000, "postSlope": 8000, "Symmetry": 0.1}
            importances = {var: 1.0 for var in self.var_names}
            mode = self._build_mode_dict(medians, mads, importances)
            return mode, 4.5

    # ---- 2. Profil laden ----
    def load_profile(self, athlet_name, logFcn=print):
        """Laedt das individuelle Baseline-Profil (zwei Modi: "aufbau"/"halten") oder
        weicht auf den globalen Goldstandard aus.

        Baseline-CSV-Format: Spalte "Mode" ("aufbau"/"halten"), pro Modus eine Zeile
        je Feature. Alte Baseline-CSVs ohne "Mode"-Spalte werden aus Rueckwaerts-
        kompatibilitaet als "halten" interpretiert (mit Log-Hinweis auf Neuberechnung);
        der Modus "aufbau" weicht in diesem Fall auf den Goldstandard aus.
        """
        baseline_path = os.path.join("athleten_daten", f"{athlet_name}_baseline.csv")
        profiles = {}
        mode_sources = {}
        h_max = None

        if athlet_name not in ["master_session_daten", "global"] and os.path.exists(baseline_path):
            try:
                df_base = pd.read_csv(baseline_path)
                if "Mode" not in df_base.columns:
                    logFcn(f"Athlet-Referenz '{athlet_name}' im alten Format – als Halten "
                           f"übernommen. Bitte einmal neu aufzeichnen zum Aktualisieren.")
                    df_base = df_base.copy()
                    df_base["Mode"] = "halten"

                for mode in ("aufbau", "halten"):
                    df_mode = df_base[df_base["Mode"] == mode]
                    if df_mode.empty:
                        continue
                    df_mode = df_mode.set_index("Feature").reindex(self.var_names)
                    if df_mode["Median"].notna().any():
                        profiles[mode] = self._build_mode_dict(
                            df_mode["Median"].fillna(0.0),
                            df_mode["MAD"].fillna(1.0),
                            df_mode["Importance"].fillna(0.0),
                        )
                        mode_sources[mode] = "individuelle Baseline"
                        h_max_vals = df_mode["H_Max"].dropna()
                        if len(h_max_vals) > 0 and h_max is None:
                            h_max = float(h_max_vals.iloc[0])
            except Exception as e:
                logFcn(f"Fehler beim Laden der Referenz von '{athlet_name}' – "
                       f"nutze Goldstandard ({e}).")
                profiles, mode_sources = {}, {}

        if "aufbau" not in profiles or "halten" not in profiles:
            gold_profile, gold_h_max = self._gold_mode(logFcn)
            for mode in ("aufbau", "halten"):
                if mode not in profiles:
                    profiles[mode] = gold_profile
                    mode_sources[mode] = "Goldstandard"
            if h_max is None:
                h_max = gold_h_max

        self.profiles = profiles
        self.mode_sources = dict(mode_sources)
        self.h_max = h_max if h_max is not None else 4.5

        src_txt = {"individuelle Baseline": "individuell", "Goldstandard": "Standard"}
        a_src = src_txt.get(mode_sources.get("aufbau"), "Standard")
        h_src = src_txt.get(mode_sources.get("halten"), "Standard")
        logFcn(f"Referenz für '{athlet_name}' geladen – Aufbau: {a_src}, "
               f"Halten: {h_src}, Besthöhe {self.h_max:.2f} m.")

    # ---- 2b. Ampel (ESP32) anbinden ----
    def set_ampel_client(self, client):
        """Setzt (oder entfernt, mit None) den AmpelClient aus esp_client.py.

        Der Analyzer sendet dann nach jedem Sprung den phasenabhaengigen
        Ampel-Zustand. Ohne Client wird der Zustand trotzdem berechnet und in
        self.last_ampel_state abgelegt (fuer GUI/Debug).
        """
        self.ampel_client = client

    # ---- 2c. Live-Dashboard (GUI) anbinden ----
    def set_on_jump(self, callback):
        """Setzt (oder entfernt, mit None) einen Per-Sprung-Callback fuer die GUI.

        Der Callback wird nach jedem erkannten Sprung mit einem dict aufgerufen:
            {"jump_no", "phase", "ampel_direction", "ampel_level"}
        Er laeuft im selben Thread wie process(); die GUI hebt ihn per Qt-Signal
        thread-sicher in den GUI-Thread (analog zu den Verbindungs-Callbacks).
        """
        self.on_jump = callback

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

    # ---- 3b. Flughoehe VOR dem aktuellen Kontakt (fuer die Phasen-Weiche) ----
    def _h_previous_for_jump(self, next_jump_idx, left):
        """Schaetzt die Flughoehe des Flugs, der in den aktuellen Kontakt fuehrt.

        Verallgemeinerte Form der frueheren High-Performance-Zone-Berechnung.
        Liefert None, wenn kein gueltiger vorheriger Kontakt bekannt ist.
        """
        if next_jump_idx > 0 and self.right_idx[next_jump_idx - 1] is not None:
            letztes_kontakt_ende = self.right_idx[next_jump_idx - 1]
            if left > letztes_kontakt_ende:
                t_flug = (left - letztes_kontakt_ende) / self.fs_file
                return 0.125 * self.g * (t_flug ** 2)
        return None

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

            # Integral ueber den GESAMTEN Kontakt + diffI (latenzfreier Praediktor fuer HG).
            integral_full = trapezoid(jump, t_seg)
            diffI = np.nan if self.last_integral is None else integral_full - self.last_integral
            self.last_integral = integral_full

            self.data["Peak"].append(peak)
            self.data["Peak_t"].append(peak_t)
            self.data["Peak_Prct"].append(peak_prct)
            self.data["timing"].append(timing)
            self.data["Explosiv"].append(explosiv)
            self.data["preSlope"].append(pre_slope)
            self.data["postSlope"].append(post_slope)
            self.data["Symmetry"].append(sym)
            self.data["Contact_t"].append(contact_t)
            self.data["Integral"].append(integral_full)
            self.data["diffI"].append(diffI)

            # ~ 4.4 Phasen-Weiche + Coaching-Logik ~
            h_previous = self._h_previous_for_jump(next_jump_idx, left)
            phase = determine_phase(h_previous, self.h_max)

            current_features = {
                "Peak_t": peak_t, "Peak_Prct": peak_prct, "Explosiv": explosiv,
                "preSlope": pre_slope, "postSlope": post_slope, "Symmetry": sym,
            }

            mode_profile = self.profiles.get(phase, self.profiles.get("halten"))

            # ZENTRALE Score-Berechnung (Importance intern auf Summe = 1.0 normiert).
            # Wird in BEIDEN Phasen berechnet und geloggt (fuer spaetere Analysen),
            # auch wenn die Coaching-AUSGABE in "aufbau" nicht timing-basiert ist.
            result = compute_jump_score(
                current_features=current_features,
                reference=mode_profile["reference"],
                deviation=mode_profile["deviation"],
                importance=mode_profile["importance_dict"],
                direction=self.direction_multiplier,
                feature_order=self.var_names,
            )

            trend_score = result["trend_score"]   # mit Richtung (+ = "frueher treten")
            abs_score = result["abs_score"]        # reine Abweichung, gewichtetes Mittel der |z|

            self.total_jump_count += 1

            # Klartext-Rueckmeldung je Phase (eine verstaendliche Zeile pro Sprung).
            if phase == "halten":
                phase_label = "Halten"
                if abs(trend_score) < DEADBAND_TREND:
                    coaching_output = "Timing stabil"
                else:
                    consistency = (abs(trend_score) / abs_score) if abs_score > 0.0 else 0.0
                    if consistency > CONSISTENCY_GATE:
                        direction_txt = "früher treten" if trend_score > 0 else "später treten"
                        coaching_output = f"{step_label(abs_score)} {direction_txt}"
                    else:
                        coaching_output = "Abweichung uneinheitlich"
            else:  # phase == "aufbau": keine Timing-Bewertung, output-orientierte Rueckmeldung.
                phase_label = "Aufbau"
                if not np.isfinite(diffI):
                    coaching_output = "erster Sprung"
                elif diffI > AUFBAU_DIFFI_THRESHOLD:
                    coaching_output = "Höhe kommt"
                else:
                    coaching_output = "mehr Druck ins Tuch"

            # Eine Zeile pro Sprung: Nr. · Phase · Klartext (kompakte Kennzahl in Sigma).
            logFcn(f"Sprung {self.total_jump_count} · {phase_label} · {coaching_output} "
                   f"(Abweichung {abs_score:.1f}σ)")

            self.data["coaching"].append(coaching_output)

            # ~ 4.5 Ampel (ESP32): Zustand aus derselben phasenabhaengigen Logik ~
            if classify_ampel is not None:
                # Richtungslichter im Aufbau nur gegen eine INDIVIDUELLE
                # Aufbau-Baseline (Goldstandard = Steady-State waere dort falsch).
                aufbau_ok = (self.mode_sources.get("aufbau") == "individuelle Baseline")
                self.last_ampel_state = classify_ampel(
                    trend_score, abs_score, phase=phase, diffI=diffI,
                    aufbau_reference_ok=aufbau_ok)
                if self.ampel_client is not None:
                    led_direction, led_level = self.last_ampel_state
                    try:
                        self.ampel_client.send_state(led_direction, led_level)
                    except Exception as e:
                        logFcn(f"Ampel: Senden fehlgeschlagen ({e}).")

            # ~ 4.6 Live-Dashboard (GUI): Schnellinfos zum aktuellen Sprung ~
            # Laeuft immer (auch ohne Ampel-Client / ohne classify_ampel); Fehler
            # im Callback duerfen die Analyse nie stoeren.
            if self.on_jump is not None:
                try:
                    self.on_jump({
                        "jump_no": self.total_jump_count,
                        "phase": phase,
                        "ampel_direction": self.last_ampel_state[0],
                        "ampel_level": self.last_ampel_state[1],
                    })
                except Exception:
                    pass

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
        self.last_integral = None
        self.last_ampel_state = ("OFF", 0)
        self.zi = lfilter_zi(self.b, self.a)
        self.data = {var: [] for var in self.log_var_names}
        self.data["coaching"] = []
