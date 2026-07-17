# session_viewer.py
"""
Nachtraegliche Visualisierung gespeicherter Analyse-Sessions (.npz).

Aufruf:
    python session_viewer.py                      -> Datei-Dialog (falls verfuegbar)
    python session_viewer.py <pfad_zur_session>   -> direkt anzeigen

Wird auch aus main.py heraus (per Button) als Subprozess gestartet, damit es
keine Konflikte zwischen dem Qt- und dem Matplotlib-Backend gibt.

Dargestellt werden:
    * Kraft-Zeit-Kurve (Rohsignal + gefiltertes Signal)
    * Peak-Markierungen
    * Kontaktphasen (left_idx -> right_idx) farblich markiert
    * Flugphasen (right_idx[i] -> left_idx[i+1]) farblich markiert
    * Beschriftung: Sprungnummer, Height, HG

Die Funktion ist robust: fehlende oder am Rand liegende Indizes werden uebersprungen.
"""

import os
import sys
import numpy as np
import matplotlib.pyplot as plt

from session_storage import load_session


def _valid_idx(i, n):
    """True, wenn i ein gueltiger Index im Bereich [0, n) ist."""
    try:
        return (i is not None) and np.isfinite(i) and (0 <= int(i) < n)
    except (TypeError, ValueError):
        return False


def plot_session(session, ax=None):
    """Zeichnet eine geladene Session (dict aus session_storage.load_session) in eine Achse."""
    raw = np.asarray(session.get("raw_signal", []), dtype=float)
    filt = np.asarray(session.get("filtered_signal", []), dtype=float)
    fs = float(session.get("fs", 1000)) or 1000.0

    peaks = np.asarray(session.get("peak_indices", []), dtype=float)
    left = np.asarray(session.get("left_indices", []), dtype=float)
    right = np.asarray(session.get("right_indices", []), dtype=float)
    heights = np.asarray(session.get("heights", []), dtype=float)
    hg = np.asarray(session.get("HG", []), dtype=float)

    n = len(filt) if len(filt) > 0 else len(raw)
    if n == 0:
        raise ValueError("Session enthaelt kein Signal.")

    t = np.arange(n) / fs

    if ax is None:
        _, ax = plt.subplots(figsize=(14, 6))

    # --- Signale ---
    if len(raw) == n:
        ax.plot(t, raw, color="#9aa0a6", lw=0.8, alpha=0.5, label="Rohsignal")
    if len(filt) == n:
        ax.plot(t, filt, color="#1f77b4", lw=1.3, label="Gefiltert")

    sig = filt if len(filt) == n else raw

    # --- Kontaktphasen (left -> right) ---
    contact_label_set = False
    for i in range(len(peaks)):
        li = left[i] if i < len(left) else None
        ri = right[i] if i < len(right) else None
        if _valid_idx(li, n) and _valid_idx(ri, n) and int(ri) > int(li):
            ax.axvspan(int(li) / fs, int(ri) / fs, color="#2ca02c", alpha=0.15,
            label=None if contact_label_set else "Kontaktphase")
            contact_label_set = True

    # --- Flugphasen (right[i] -> left[i+1]) ---
    flight_label_set = False
    for i in range(len(peaks) - 1):
        ri = right[i] if i < len(right) else None
        lj = left[i + 1] if (i + 1) < len(left) else None
        if _valid_idx(ri, n) and _valid_idx(lj, n) and int(lj) > int(ri):
            ax.axvspan(int(ri) / fs, int(lj) / fs, color="#ff7f0e", alpha=0.12,
            label=None if flight_label_set else "Flugphase")
            flight_label_set = True

    # --- Peak-Markierungen + Beschriftung ---
    peak_label_set = False
    y_top = np.nanmax(sig) if np.isfinite(np.nanmax(sig)) else 1.0
    for i in range(len(peaks)):
        pi = peaks[i]
        if not _valid_idx(pi, n):
            continue
        pi = int(pi)
        ax.plot(pi / fs, sig[pi], "v", color="#d62728", markersize=8,
                label=None if peak_label_set else "Peak")
        peak_label_set = True

        # Beschriftung: Sprungnummer + Height + HG (falls vorhanden)
        parts = [f"#{i + 1}"]
        if i < len(heights) and np.isfinite(heights[i]):
            parts.append(f"H={heights[i]:.2f}m")
        if i < len(hg) and np.isfinite(hg[i]):
            parts.append(f"HG={hg[i]:+.2f}m")
        ax.annotate(" ".join(parts), xy=(pi / fs, sig[pi]),
                    xytext=(0, 12), textcoords="offset points",
                    ha="center", fontsize=8, color="#333333",
                    bbox=dict(boxstyle="round,pad=0.2", fc="#ffffff", ec="#cccccc", alpha=0.8))

    athlete = session.get("athlete_name", "?")
    ts = session.get("timestamp", "?")
    tramp = session.get("selected_trampoline", "?")
    ax.set_title(f"Session: {athlete}  |  {ts}  |  Trampolin {tramp}")
    ax.set_xlabel("Zeit [s]")
    ax.set_ylabel("Kraft [a.u.]")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right", fontsize=9)
    return ax


def show_session(path):
    """Laedt eine Session-Datei und zeigt sie als Matplotlib-Fenster an."""
    if not os.path.exists(path):
        print(f"Datei nicht gefunden: {path}")
        return
    session = load_session(path)
    plot_session(session)
    plt.tight_layout()
    plt.show()


def _pick_file_dialog():
    """Oeffnet einen einfachen Datei-Dialog (Tkinter). Gibt Pfad oder None zurueck."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        start_dir = os.path.join("athleten_daten", "sessions")
        if not os.path.isdir(start_dir):
            start_dir = "."
        path = filedialog.askopenfilename(
            title="Session-Datei waehlen",
            initialdir=start_dir,
            filetypes=[("Session-Dateien", "*.npz"), ("Alle Dateien", "*.*")],
        )
        root.destroy()
        return path or None
    except Exception as e:
        print(f"Kein Datei-Dialog verfuegbar ({e}).")
        return None


if __name__ == "__main__":
    if len(sys.argv) > 1:
        show_session(sys.argv[1])
    else:
        picked = _pick_file_dialog()
        if picked:
            show_session(picked)
        else:
            print("Kein Pfad angegeben. Aufruf: python session_viewer.py <pfad_zur_session.npz>")
