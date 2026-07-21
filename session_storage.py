# session_storage.py
"""
Speichert die vollstaendige Analysekurve eines Durchgangs als .npz-Datei.

Ablageort:
    athleten_daten/sessions/<athlet_name>/<timestamp>_session.npz

Die Datei enthaelt alles, was fuer eine spaetere Visualisierung noetig ist
(siehe session_viewer.py). Das Speichern wird beim Stoppen der Analyse automatisch
aus main.py heraus aufgerufen.
"""

import os
import re
import numpy as np
from datetime import datetime

from profiler import analyze_raw_signal


def _safe_name(name):
    """Macht einen Athleten-Namen Dateisystem-/Windows-kompatibel."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", str(name)).strip()
    return cleaned if cleaned else "unbekannt"


def save_session(raw_signal, athlet_name, fs, peak_height, peak_distance, a, b,
    selected_trampoline, timestamp=None):
    """Analysiert das Rohsignal und speichert die komplette Session als .npz.

    Rueckgabe: (pfad_oder_None, statusmeldung)
    """
    try:
        if raw_signal is None or len(raw_signal) == 0:
            return None, "Keine Daten vorhanden – Session nicht gespeichert."

        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = timestamp

        result = analyze_raw_signal(raw_signal, fs, peak_height, peak_distance, a, b)

        safe_athlet = _safe_name(athlet_name)
        session_dir = os.path.join("athleten_daten", "sessions", safe_athlet)
        os.makedirs(session_dir, exist_ok=True)
        out_path = os.path.join(session_dir, f"{timestamp}_session.npz")

        np.savez_compressed(
            out_path,
            raw_signal=result["raw_signal"],
            filtered_signal=result["filtered_signal"],
            fs=np.array(fs),
            peak_indices=result["peaks"],
            left_indices=result["left_idx"],
            right_indices=result["right_idx"],
            heights=result["heights"],
            HG=result["hg"],
            selected_trampoline=np.array(str(selected_trampoline)),
            athlete_name=np.array(str(athlet_name)),
            session_id=np.array(str(session_id)),
            timestamp=np.array(str(timestamp)),
        )

        n_jumps = len(result["peaks"])
        return out_path, f"Session gespeichert ({n_jumps} Sprünge)."

    except Exception as e:
        return None, f"Fehler beim Speichern der Session ({str(e)})."


def load_session(path):
    """Laedt eine gespeicherte Session-.npz und gibt ein dict mit allen Feldern zurueck.

    Skalare/String-Felder werden aus den 0-dim-Arrays ausgepackt.
    """
    data = np.load(path, allow_pickle=False)
    out = {}
    for key in data.files:
        arr = data[key]
        if arr.ndim == 0:
            val = arr.item()
            out[key] = val.decode() if isinstance(val, bytes) else val
        else:
            out[key] = arr
    return out
