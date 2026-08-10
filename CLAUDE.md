# HDTS – Kontext für Claude

LED-Ampel-Feedback für Kinder auf dem Trampolin. Eine Kraftmessplatte misst den Bodenkontakt;
während der anschließenden Flugphase zeigt eine ESP32-Ampel eine Rückmeldung zum **letzten**
Kontakt, damit das Kind sie beim nächsten umsetzen kann. Zwei Trainingsziele: **erst Höhe
aufbauen, dann Höhe halten.**

Arbeitslösung für die Trainingspraxis, keine wissenschaftliche Arbeit. Die Kraftmessplatte ist
das Instrument — die Kamera war nur Validierungswerkzeug und läuft im Betrieb nicht mit.

---

## Autoritative Quellen

| Frage | Wo steht die Antwort |
|---|---|
| Befunde, Zielzustand, offene Entscheidungen | `docs/HDTS_Uebergabe.md` (Stand 06.08.2026) |
| Aktuelle Parameterwerte + Begründung | `importance_utils.py` (Kommentare sind ausführlich) |
| Ampel-/Text-Entscheidungsbaum | Docstring von `esp_client.decide_feedback` |

**Regel: Keine Abschnittsnummern des Übergabedokuments zitieren, ohne es gelesen zu haben.**
Genau das ist schon schiefgegangen — aus dem Gedächtnis wurde die falsche Zeile von §5.4
zitiert (Live-Test statt Validierungsdatensatz) und daraus ein falscher Zielwert abgeleitet.

---

## Architektur in Kurzform

Pro Kontakt werden 6 Features berechnet und gegen eine phasenabhängige Referenz gescort:

```
Rohsignal → JumpAnalyzer.process()  (48er-Blöcke, inkrementelle Kontakterkennung)
          → 6 Features
          → Phase aus HG-Dynamik (stateful, Hysterese)
          → Referenz für diese Phase (Rolling-Fenster, sonst Warmstart aus Baseline)
          → importance_utils.compute_jump_score() → trend_score, abs_score
          → esp_client.decide_feedback() → (direction, level, text)
          → LED bekommt (direction, level), Log bekommt text — aus EINER Entscheidung
```

**Score-Features (6, nach Bereinigung):** `Peak_t, Peak_Prct, Explosiv, preSlope, postSlope, Symmetry`
**Richtungen:** `Peak_t +1, Peak_Prct +1, Symmetry +1, postSlope +1, preSlope −1, Explosiv −1`
`Peak` und `timing` werden weiter berechnet und geloggt, aber **nicht** gescort (`timing` war
exakt `100 × Peak_t` → perfekte Kollinearität).

**Zentraler Vertrag:** `decide_feedback(trend, abs, phase, diffI, aufbau_reference_ok)`
gibt `(direction, level, text)` zurück. Text und LED stammen zwingend aus demselben Aufruf.
`classify_ampel` ist nur noch ein dünner Wrapper für Rückwärtskompatibilität.
Protokoll zur Firmware: `SHOW EARLY <1..3>` / `SHOW LATE <1..3>` / `SHOW GOOD` / `OFF`.
Die Namen `EARLY`/`LATE` sind firmwareseitig fix — Wording-Änderungen betreffen nur die GUI.

---

## Entschieden **und** gebaut

| Thema | Stand |
|---|---|
| Text- und LED-Pfad zusammengeführt | `decide_feedback` — behebt die einzige bestätigte Logikfehler-Divergenz |
| `DIFFI_DEADBAND = 22.0` zentralisiert | ersetzt das doppelt definierte `AUFBAU_DIFFI_THRESHOLD` |
| Phase aus HG-Dynamik statt `h_prev/h_max` | asymmetrische Hysterese: Einstieg sofort, Ausstieg nach 2 ruhigen Sprüngen |
| Rolling-Referenz (12 Kontakte, ab 6) | ersetzt den Gold-Fallback im Halten |
| MAD-Floor `max(MAD, 0.30 × GoldStd)` | kappt z-Explosion aus engen Teilmengen |
| Kein Gold-Fallback im Aufbau | unter 15 Aufbau-Sprüngen nur das diffI-Kriterium |

Werte stehen in `importance_utils.py` — **dort ist die Wahrheit**, nicht hier.

## Entschieden, aber **noch nicht gebaut**

- **`PEAK_PRCT_SAFETY_MIN = 25.0`** — Sicherung gegen „immer steifer": unter ~25 % `Peak_Prct`
  kein GELB mehr, sondern GRÜN oder AUS. Im Übergabedokument beschlossen (§5.3/§6), im Code
  **nirgends vorhanden**. Reine Vorsichtsmaßnahme, die Schwelle wurde empirisch nie erreicht.

## Offen — nicht eigenmächtig entscheiden

- **§7.1 Totband quantilbasiert** (wichtigster offener Punkt). Fixes `DEADBAND_TREND = 0.5`
  wirkt je Athlet völlig verschieden: Feedback-Rate 33 % beim Live-Test, aber nur 12 % auf der
  Validierungsgruppe. Kalibrierkurve dort: 0.25 → 34 %, 0.30 → 30 %, 0.40 → 20 %, 0.50 → 12 %.
  Vorschlag im Dokument: P70 der letzten 12–20 eigenen trend-Werte.
- **§7.2 Übergangssprünge** am Ende einer Aufbau-Episode (Phase hängt 2–3 Sprünge nach).
- **§7.3 Kontrollierter Höhenabbau** — soll die Ampel beim absichtlichen Abbremsen stumm sein?
- **§7.4 Wording.** ⚠️ Das Dokument argumentiert in §3.6/§5.3 ausdrücklich **gegen** „früher/
  später treten": der Score reagiert auf **Winkel**, nicht auf Zeitpunkte — gemeint ist
  „steifer/weicher landen". **Der Nutzer hat entschieden, das alte Wording vorerst zu
  behalten.** Diese Entscheidung überstimmt das Dokument und steht.
- **§7.6 Importance je Phase** — `|corr(Feature, HG)|` lernt Aufbau-Prädiktoren; fürs Halten
  wäre `|corr(Feature, Height)|` passender.

---

## Daten

**`athleten_daten/` ist gitignored** und im Cloud-Container leer. Profile existieren nur lokal
beim Nutzer — dort liegt **pro Athlet ein eigenes Profil**. Keine Klarnamen ins Repo
(öffentliches GitHub).

| Datei | Umfang | Inhalt |
|---|---|---|
| `data/hdts_knie_gesamt.xlsx` | **139 Zeilen** | 7 Personen / 8 Serien, HDTS-Features + Video + 17 Kniewinkel-Spalten |
| `athleten_daten/louis_all.csv` | 103 Sprünge | Live-Prototyp, 2 Sessions (68 + 35), 1 Erwachsener, bewusst chaotisch |

Serien-Reihenfolge in den 139 Zeilen: maya 1–18, valeria 19–37, lydia 38–57, johanna1 58–69,
johanna2 70–87, jonas 88–105, jannick 106–121, julian 122–139.
**johanna1/2 ist dieselbe Person** (2 Serien) → 7 Personen. `julian` ist der frühere Ausreißer.

**Zeilenzahlen vor der Interpretation prüfen.** Wenn eine Auswertung eine andere Sprungzahl
meldet als hier steht, wurden mehrere Dateien aggregiert — `tools/simulate_feedback.py` druckt
„GESAMT (alle Dateien)" **nur** bei mehr als einer Datei. Das wurde schon einmal übersehen.

---

## Referenzwerte aus §5.4 — Datensatz immer mitnennen

Die beiden Zeilen sind **nicht** vergleichbar. Wer sie verwechselt, kalibriert in die falsche Richtung.

| | Live-Test (103 Sprünge, 1 Erwachsener) | Validierung (139 Sprünge, 8 Serien) |
|---|---|---|
| Licht an | 71 % | – |
| Halten grün / gelb / blau / aus | 67 / 25 / 0 / 9 % | 88 / 1 / 10 / – % |
| trend Median | – | **−0.04** |
| trend negativ | – | **58 %** |
| abs Median | – | **0.27** |

Auf der Validierungsgruppe ist der Blau-Anteil **größer** als der Gelb-Anteil. Ein Lauf mit
starkem Gelb-Überhang und positivem trend-Median weist auf eine Referenz hin, die nicht der
eigene Körper ist — genau das Muster aus §5.1.3 (Gold-Referenz → trend konstant +1.2 bis +2.6 σ).

---

## Fallstricke

**Rolling-Referenz über Serien-/Athletengrenzen hinweg verfälscht alles.** Die
Zwischen-Athleten-Streuung ist laut §3.7 das **2.6- bis 10.4-Fache** der Within-Streuung.
Sobald das 12er-Fenster mischt, ist die Referenz eine Mittelung über fremde Körper: `abs`
steigt, `trend` verschiebt sich systematisch. Bei jeder Offline-Auswertung sicherstellen, dass
pro Serie neu begonnen wird.

**80 % „aus" im Aufbau ist kein Bug.** Ohne individuelle Aufbau-Baseline
(`aufbau_reference_ok = False`) sieht `decide_feedback` per Design nur das diffI-Kriterium:
GRÜN bei `diffI > 22`, sonst AUS — Richtungslichter erst, wenn die Aufbau-Baseline steht. Ein
Wert von „Richtung 0 %" in einem Report beweist, dass ohne individuelle Aufbau-Baseline
gelaufen wurde.

**Das Aufbau-Paradox** (§3.2) ist der Grund für die gesamte Zwei-Phasen-Architektur: der
Kontakt, der Höhe erzeugt, ist der lange, weiche „Lade-Kontakt", der vom Profi-Muster
**abweicht**. Ein schlechter Score kann im Aufbau also richtig sein. Nie beide Phasen gegen
dieselbe Referenz bewerten.

**Der Score misst Landesteifigkeit** (§3.6), keine Zeitpunkte. „Ampel zeigt früher" heißt
mechanisch: gestreckter/steifer landen.

**`johanna1_neu.xlsx` hat eine defekte Zeitachse** (Werte laufen rückwärts, Abstände doppelt) —
die 11 Kurven sind ausgeschlossen.

**Cloud-Container vs. lokal:** Der Nutzer arbeitet lokal unter Windows und pusht von dort. Der
Container-Klon kann hinterherhinken — vor Aussagen über den Repo-Zustand `git fetch` und den
Remote-Stand prüfen, nicht den lokalen Klon für maßgeblich halten.

---

## Repo-Struktur

```
importance_utils.py   Konstanten + compute_jump_score  ← einzige Wahrheit für Parameter
jump_analyzer.py      Live-Pfad: process(), Phasenerkennung, Rolling-Referenz
esp_client.py         decide_feedback() + Ampel-Transport (USB-Serial / WLAN-TCP)
baseline_manager.py   Baseline je Athlet, zwei Referenzsätze (aufbau/halten)
profiler.py           Offline-Auswertung, analyze_raw_signal()
main.py               PyQt6-GUI
goldTableNeu.xlsx     Goldstandard-Referenz (Profi-Muster)
docs/                 HDTS_Uebergabe.md — autoritatives Übergabedokument
data/                 Validierungsdaten (hdts_knie_gesamt.xlsx)
tools/                simulate_feedback.py, test_refactor.py
athleten_daten/       gitignored: <name>_all.csv, <name>_baseline.csv,
                      sessions/<name>/<timestamp>_session.npz
ampel_firmware/       ESP32 (PlatformIO) — wird nicht geändert
```

## Verifikation

```bash
python tools/test_refactor.py                      # 9 Tests (a–i), müssen alle grün sein
python tools/simulate_feedback.py <pfad|ordner> [--profile NAME] [--live-check]
```

Die Testdatei muss die Fassung mit **a–i** sein. Eine ältere Fassung mit nur a–f ist im Umlauf;
ihr fehlen genau die Tests für diffI-Totband, Hysterese-Phase und Rolling-Referenz/MAD-Floor —
also für die zuletzt gebauten Teile. Bei nur 6 Tests: veraltete Datei, nicht weiterverwenden.

`simulate_feedback.py` treibt die **echten Produktionspfade** offline an (kein Nachbau):
Phasenerkennung + Rolling-Referenz aus `JumpAnalyzer`, `compute_jump_score`, `decide_feedback`.
`--profile` steuert den Warmstart (Default `global` = Goldstandard; besser: der Athletenname,
damit nicht gegen Gold warmgestartet wird). `--live-check` schickt `.npz`-Rohsignale zusätzlich
durch `process()` in 48er-Blöcken; kleine Randabweichungen von 1–2 Sprüngen sind normal, weil
die inkrementelle Kontakterkennung anders segmentiert als die Offline-Variante.

Vor jeder Parameteränderung: erst die Datenlage klären (welche Dateien, welches Profil, pro
Serie getrennt?), dann rechnen. Die Zielwerte oben sind der Maßstab — mit Datensatz-Label.
