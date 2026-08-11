"""Baut athleten_daten/<name>_baseline.csv aus der jeweiligen <name>_all.csv.

Kurzes Hilfsskript, kein Teil der Produktionslogik: ruft fuer jeden gefundenen
Athleten baseline_manager.update_athlete_baseline() auf (dieselbe Funktion, die
die GUI beim "Baseline aktualisieren"-Knopf verwendet) und druckt das Ergebnis.

Laeuft NUR lokal sinnvoll - athleten_daten/ ist gitignored und im Cloud-
Container leer.

Aufruf:
    python tools/build_baselines.py                  # alle *_all.csv in athleten_daten/
    python tools/build_baselines.py <name> ...        # nur bestimmte Athleten
"""
import glob
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

import baseline_manager

SKIP = {"master_session_daten", "global", "test", "test_test", "video_test"}


def discover_athletes():
    names = []
    for path in sorted(glob.glob(os.path.join("athleten_daten", "*_all.csv"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        name = stem[:-4] if stem.endswith("_all") else stem
        if name not in SKIP:
            names.append(name)
    return names


def main():
    names = sys.argv[1:] if len(sys.argv) > 1 else discover_athletes()
    if not names:
        print("Keine *_all.csv in athleten_daten/ gefunden.")
        return 1

    print(f"Baue Baselines fuer {len(names)} Athlet(en): {', '.join(names)}")
    print("=" * 74)
    for name in names:
        msg = baseline_manager.update_athlete_baseline(name)
        print(f"{name}: {msg}")
    print("=" * 74)
    print("Fertig. Zur Kontrolle:")
    print("  python tools/simulate_feedback.py athleten_daten --profile auto "
          "--exclude \"test*\" --exclude \"video_test*\" --exclude \"master_session_daten*\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
