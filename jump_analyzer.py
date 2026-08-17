import os
from collections import deque
import numpy as np
import pandas as pd
from scipy.signal import butter, lfilter, lfilter_zi, find_peaks
from scipy.integrate import trapezoid

from importance_utils import (
    normalize_importance, compute_jump_score, MAD_CONSISTENCY,
    ROLL_N, MIN_ROLL, MAD_FLOOR_FACTOR,
    HG_AUFBAU_ENTRY, HG_AUFBAU_MEAN3, HYSTERESE_EXIT,
    DEADBAND_WINDOW, effective_deadband, MIN_VALID_FEATURES,
)

# Feedback-Entscheidung (LED + Text in EINER Funktion) aus esp_client. Optional -
# der Analyzer laeuft auch ohne esp_client (dann ohne Richtungsansage).
try:
    from esp_client import decide_feedback
except Exception:
    decide_feedback = None


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

        # --- Live-Phasenerkennung aus HG-Dynamik (mit Hysterese) ---
        # HG[i] = h_previous[i] - h_previous[i-1] (Hoehengewinn ueber Kontakt i-1).
        self._phase = "aufbau"        # Startphase konservativ (keine Timing-Bewertung)
        self._quiet_count = 0         # aufeinanderfolgende "ruhige" Spruenge (fuer Ausstieg)
        self._last_h_previous = None  # Einflug-Hoehe des vorigen Kontakts
        self._hg_series = []          # abgeschlossene HG-Werte in Reihenfolge

        # --- Rolling-Referenz je Phase (letzte ROLL_N phasengleichen Kontakte) ---
        self._roll = {"aufbau": deque(maxlen=ROLL_N), "halten": deque(maxlen=ROLL_N)}
        # GoldStd je Feature fuer den MAD-Floor (wird in load_profile gefuellt).
        self._gold_std = {}

        # --- Quantil-Totband je Phase (Uebergabe 7.1) ---------------------
        # trend-Werte der letzten Kontakte, aus denen das wirksame Totband als
        # Quantil gebildet wird. NUR Kontakte, die gegen die EIGENE Referenz
        # gescort wurden - Gold-Werte liegen konstant bei +1.2..+2.6 Sigma und
        # wuerden das Quantil aufblasen. Je Phase getrennt, weil Aufbau- und
        # Halten-Kontakte voellig verschiedene trend-Verteilungen haben.
        self._trend_hist = {"aufbau": deque(maxlen=DEADBAND_WINDOW),
                            "halten": deque(maxlen=DEADBAND_WINDOW)}

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

    def _load_gold_std(self, logFcn=print):
        """Liest GoldStd je Feature (fuer den MAD-Floor). Bei Fehler leeres dict.

        Faellt der Read aus, ist _apply_mad_floor ein reiner Durchlauf - die
        Absicherung gegen z-Explosion aus sehr engen Session-MADs fehlt dann
        still. Deshalb wird jeder Ausfall EINMAL beim Laden des Profils
        gemeldet (nicht pro Sprung), sowohl der komplette als auch einzelne
        Features ohne gueltigen GoldStd. Rein informativ: kein Abbruch, kein
        Ersatz-Floor.
        """
        try:
            gold = pd.read_excel("goldTableNeu.xlsx").set_index("Feature").reindex(self.var_names)
            out = {var: float(gold.loc[var, "GoldStd"]) for var in self.var_names
                   if np.isfinite(gold.loc[var, "GoldStd"])}
        except Exception as e:
            logFcn(f"Warnung: Standard-Streuungen nicht lesbar ({e}). MAD-Floor ist "
                   f"inaktiv – Streuungen werden nicht nach unten begrenzt.")
            return {}

        fehlend = [var for var in self.var_names if var not in out]
        if fehlend:
            logFcn(f"Warnung: Für {', '.join(fehlend)} fehlt eine Standard-Streuung. "
                   f"Für diese Merkmale greift der MAD-Floor nicht.")
        return out

    # ---- 1c. MAD-Floor + Referenzaufbau (Rolling ODER Warmstart) ----
    def _apply_mad_floor(self, deviation):
        """deviation = max(MAD, MAD_FLOOR_FACTOR * GoldStd) je Feature.

        Kappt die z-Explosion aus kleinen, homogenen Teilmengen (individuelle MADs
        koennen absurd eng werden, z.B. Peak_t-MAD ~ Messaufloesung)."""
        out = {}
        for f, d in deviation.items():
            gstd = self._gold_std.get(f)
            if gstd is not None and np.isfinite(gstd) and gstd > 0:
                out[f] = max(float(d), MAD_FLOOR_FACTOR * float(gstd))
            else:
                out[f] = float(d)
        return out

    def _reference_for(self, phase):
        """Baut die Score-Referenz fuer die aktuelle Phase.

        Ab MIN_ROLL phasengleichen Kontakten im rollierenden Fenster: Median/MAD aus
        dem Fenster (tagesformrobust, loest den Gold-Fallback-Schiefstand). Davor
        Warmstart aus der gespeicherten Baseline. Importance stammt in beiden Faellen
        aus der gespeicherten Baseline. MAD-Floor wird immer angewandt.

        Je Feature gilt im Rolling-Fall die Kette
            Fenster -> gespeicherte Baseline -> Feature weglassen.
        Ein weggelassenes Feature fehlt in "reference"/"deviation" und wird von
        compute_jump_score aussortiert; die Gewichte der uebrigen skalieren neu
        auf 1.0. Das ist bewusst so: keine Referenz heisst keine Bewertung.
        """
        stored = self.profiles.get(phase) or self.profiles.get("halten")
        window = self._roll.get(phase)
        if window is not None and len(window) >= MIN_ROLL:
            ref, dev = {}, {}
            for f in self.var_names:
                vals = np.array([row[f] for row in window
                                 if f in row and np.isfinite(row[f])], dtype=float)
                # Fallback-Kette je Feature: Rolling-Fenster -> gespeicherte
                # Baseline -> Feature ganz weglassen. NIE auf 0.0/1.0 ausweichen:
                # ein Feature, das im Fenster nie gueltig war, haette dann
                # delta = (cur - 0) / 1.0 - bei Explosiv (Groessenordnung ~15000)
                # sind das vierstellige z-Werte, die trend und abs komplett
                # dominieren. Fehlt eine Referenz, ist "nicht bewerten" richtig,
                # nicht "gegen 0 bewerten"; compute_jump_score filtert Features
                # ohne Referenz automatisch aus und skaliert die Gewichte neu.
                med = mad = None
                if len(vals) > 0:
                    med = float(np.median(vals))
                    mad = float(np.median(np.abs(vals - med))) * MAD_CONSISTENCY

                if med is not None and mad is not None and mad > 0:
                    ref[f], dev[f] = med, mad
                    continue

                # Fenster unbrauchbar (kein gueltiger Wert oder MAD = 0):
                # gespeicherte Baseline verwenden, falls sie das Feature kennt.
                s_ref = stored["reference"].get(f, np.nan)
                s_dev = stored["deviation"].get(f, np.nan)
                try:
                    s_ref, s_dev = float(s_ref), float(s_dev)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(s_ref) and np.isfinite(s_dev) and s_dev > 0:
                    # Median darf aus dem Fenster stammen, wenn er gueltig ist -
                    # nur die Streuung kommt dann aus der Baseline.
                    ref[f] = med if med is not None else s_ref
                    dev[f] = s_dev
            # Rolling: die Referenz besteht aus Kontakten DIESER Session, also aus
            # dem eigenen Koerper -> Richtungsansagen sind hier gueltig.
            return {"reference": ref,
                    "deviation": self._apply_mad_floor(dev),
                    "importance_dict": stored["importance_dict"],
                    "is_own": True}
        # Warmstart: gespeicherte Baseline, aber mit MAD-Floor. Nur wenn diese
        # Baseline individuell ist, beschreibt sie den eigenen Koerper; steht dort
        # der Goldstandard, ist sie ein fremdes Muster und traegt keine Richtung.
        return {"reference": stored["reference"],
                "deviation": self._apply_mad_floor(dict(stored["deviation"])),
                "importance_dict": stored["importance_dict"],
                "is_own": self.mode_sources.get(phase) == "individuelle Baseline"}

    # ---- 1d. Live-Phase aus der HG-Dynamik (asymmetrische Hysterese) ----
    def _update_phase_from_hg(self):
        """Aktualisiert self._phase anhand der bisher abgeschlossenen HG-Werte.

        Einstieg "aufbau": letzter HG > HG_AUFBAU_ENTRY ODER Mittel der letzten 3 HG
        > HG_AUFBAU_MEAN3. Ausstieg "halten" erst nach HYSTERESE_EXIT ruhigen
        Spruengen -> kein Flattern, unabhaengig von der absoluten Hoehe.
        """
        hg = self._hg_series
        entry = False
        if hg:
            last_hg = hg[-1]
            mean3 = float(np.mean(hg[-3:]))
            entry = (last_hg > HG_AUFBAU_ENTRY) or (mean3 > HG_AUFBAU_MEAN3)
        if entry:
            self._phase = "aufbau"
            self._quiet_count = 0
        elif self._phase == "aufbau":
            self._quiet_count += 1
            if self._quiet_count >= HYSTERESE_EXIT:
                self._phase = "halten"
                self._quiet_count = 0
        return self._phase

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
        # GoldStd je Feature fuer den MAD-Floor bereitstellen. logFcn mitgeben,
        # damit ein Ausfall des Floors nicht still bleibt (einmal beim Laden).
        self._gold_std = self._load_gold_std(logFcn)

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

            # ~ 4.4 Phasen-Weiche (HG-Dynamik) + Referenz + Feedback ~
            # Einflug-Hoehe VOR diesem Kontakt; daraus der ueber den VORIGEN Kontakt
            # abgeschlossene Hoehengewinn HG (fuer die Phasenerkennung, latenzarm).
            h_previous = self._h_previous_for_jump(next_jump_idx, left)
            if h_previous is not None and np.isfinite(h_previous):
                if self._last_h_previous is not None and np.isfinite(self._last_h_previous):
                    self._hg_series.append(h_previous - self._last_h_previous)
                self._last_h_previous = h_previous
            phase = self._update_phase_from_hg()
            phase_label = "Aufbau" if phase == "aufbau" else "Halten"

            current_features = {
                "Peak_t": peak_t, "Peak_Prct": peak_prct, "Explosiv": explosiv,
                "preSlope": pre_slope, "postSlope": post_slope, "Symmetry": sym,
            }

            # Referenz: Rolling (letzte ROLL_N phasengleichen Kontakte) oder Warmstart
            # aus der gespeicherten Baseline, jeweils mit MAD-Floor.
            mode_profile = self._reference_for(phase)
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

            # EINE Entscheidung fuer Text UND LED (keine Divergenz mehr). Richtungslichter
            # im Aufbau nur gegen eine INDIVIDUELLE Aufbau-Baseline (Gold waere falsch).
            # Richtungslichter setzen eine EIGENE Referenz voraus: entweder das
            # gefuellte Rolling-Fenster dieser Session oder eine individuelle
            # gespeicherte Baseline. Gegen den Goldstandard (fremder Koerper) zeigt
            # die Ampel stattdessen GRUEN, bis die eigene Referenz steht. Das gilt
            # fuer beide Phasen und ersetzt die frueher nur an der gespeicherten
            # Aufbau-Baseline haengende Sperre.
            ref_is_own = bool(mode_profile.get("is_own", True))
            # Totband aus der eigenen juengsten trend-Verteilung dieser Phase
            # (Uebergabe 7.1). Vor DEADBAND_MIN_N eigenen Werten liefert
            # effective_deadband den Fixwert - kein Sonderfall noetig.
            band = effective_deadband(self._trend_hist[phase])
            n_used = int(result.get("n_used", len(self.var_names)))
            if n_used < MIN_VALID_FEATURES:
                # Zu wenige bewertbare Features (compute_jump_score liefert dann
                # Scores 0.0). Hier bewusst NICHT ueber reference_is_own gehen -
                # das beschreibt die HERKUNFT der Referenz, nicht die Datenlage.
                # Stattdessen direkt GRUEN: sichtbar, aber ohne Richtungsansage.
                direction, level, coaching_output = ("GOOD", 0, "zu wenig Messwerte")
            elif decide_feedback is not None:
                direction, level, coaching_output = decide_feedback(
                    trend_score, abs_score, phase=phase, diffI=diffI,
                    aufbau_reference_ok=ref_is_own,
                    reference_is_own=ref_is_own,
                    deadband=band)
            else:
                direction, level, coaching_output = ("OFF", 0, "kein Signal")
            self.last_ampel_state = (direction, level)

            # Eine Zeile pro Sprung: Nr. · Phase · Klartext (kompakte Kennzahl in Sigma).
            logFcn(f"Sprung {self.total_jump_count} · {phase_label} · {coaching_output} "
                   f"(Abweichung {abs_score:.1f}σ)")
            self.data["coaching"].append(coaching_output)

            # LED setzen (identisch zum Text, da aus derselben Entscheidung).
            if self.ampel_client is not None:
                try:
                    self.ampel_client.send_state(direction, level)
                except Exception as e:
                    logFcn(f"Ampel: Senden fehlgeschlagen ({e}).")

            # Live-Dashboard (GUI): Schnellinfos; Fehler duerfen die Analyse nie stoeren.
            if self.on_jump is not None:
                try:
                    self.on_jump({
                        "jump_no": self.total_jump_count,
                        "phase": phase,
                        "ampel_direction": direction,
                        "ampel_level": level,
                    })
                except Exception:
                    pass

            # Aktuellen Kontakt NACH dem Scoren ins phasengleiche Rolling-Fenster legen.
            self._roll[phase].append(current_features)

            # trend NUR dann ins Totband-Fenster, wenn gegen die EIGENE Referenz
            # gescort wurde. Gold-Warmstart-Werte (+1.2..+2.6 Sigma) wuerden das
            # Quantil aufblasen und die Ampel danach stumm schalten.
            if ref_is_own and np.isfinite(trend_score):
                self._trend_hist[phase].append(float(trend_score))

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
        # Phasen- und Rolling-Referenz-Zustand fuer die neue Session zuruecksetzen.
        self._phase = "aufbau"
        self._quiet_count = 0
        self._last_h_previous = None
        self._hg_series = []
        self._roll = {"aufbau": deque(maxlen=ROLL_N), "halten": deque(maxlen=ROLL_N)}
        self._trend_hist = {"aufbau": deque(maxlen=DEADBAND_WINDOW),
                            "halten": deque(maxlen=DEADBAND_WINDOW)}
        self.zi = lfilter_zi(self.b, self.a)
        self.data = {var: [] for var in self.log_var_names}
        self.data["coaching"] = []
