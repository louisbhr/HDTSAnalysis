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

**Zentraler Vertrag:** `decide_feedback(trend, abs, phase, diffI, aufbau_reference_ok,
reference_is_own, deadband)` gibt `(direction, level, text)` zurück. Text und LED stammen zwingend aus demselben Aufruf.
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
| **Gold-Warmstart zeigt GRÜN** | keine Richtung und kein AUS, solange gegen Gold gescort wird (beide Phasen) |
| **§7.1 Quantil-Totband** | `DEADBAND_MODE = "quantil"`: P70 der letzten 20 **eigenen** trend-Werte je Phase, ab 8 Werten, Leitplanken 0.25/1.20 |

Werte stehen in `importance_utils.py` — **dort ist die Wahrheit**, nicht hier.

**Zum Quantil-Totband** (`effective_deadband`): Nur Kontakte, die gegen die **eigene** Referenz
gescort wurden, kommen ins Fenster — Gold-Werte liegen konstant bei +1.2 bis +2.6 σ und würden
das Quantil aufblasen. Je Phase getrennt. `decide_feedback` bleibt zustandslos und bekommt das
Band als Parameter `deadband=`; ohne Angabe gilt weiter der Fixwert. Zurückschalten: einzig
`DEADBAND_MODE = "fix"`.

## Entschieden, aber **noch nicht gebaut**

- **`PEAK_PRCT_SAFETY_MIN = 25.0`** — Sicherung gegen „immer steifer": unter ~25 % `Peak_Prct`
  kein GELB mehr, sondern GRÜN oder AUS. Im Übergabedokument beschlossen (§5.3/§6), im Code
  **nirgends vorhanden**. Reine Vorsichtsmaßnahme, die Schwelle wurde empirisch nie erreicht.

## Offen — nicht eigenmächtig entscheiden

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

**`.npz` und `_all.csv` sind dieselben Sprünge — ein rekursiver Ordnerlauf zählt doppelt.**
Belegt am 11.08.: ein Lauf über `athleten_daten/` meldete **707** Sprünge aus 37 Dateien.
Rechnung: alle `_all.csv` ergeben 345, alle `.npz` unter `sessions/` ergeben 362, Summe 707.
Beweis der Identität: `jannick-scheibler_all.csv` und `20260703_161633_session.npz` liefern
beide n = 18 und `trend_median` **bitgenau** `1.8360772695937175`; dasselbe bei `julian` und
`lydia`. **Konsequenz:** `--recurse` ist seither nicht mehr Default, und der Report warnt, wenn
es gesetzt ist. Für einen Ordnerlauf gilt: `_all.csv` **oder** `.npz`, nie beides.

**Testdateien gehören nicht in die Kennzahlen.** In `athleten_daten/` liegen neben den echten
Athleten auch `test*`, `video_test*` und `master_session_daten*`. Mit `--exclude` ausschließen.

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

**Der Gold-Warmstart erzeugte exakt MIN_ROLL Gelb-Sprünge — belegt, behoben.**
Bei der Auswertung der Athletendateien (10.08.) hatten **5 von 7** Dateien im Halten exakt
**6 GELB** — die Zahl ist `MIN_ROLL`. Es sind genau die Kontakte vor dem Füllen des
Rolling-Fensters, gescort gegen den Goldstandard. Bei `jannick` und `jonas` war
`trend_median` **bitgenau gleich** `abs_median` (Konsistenz = 1) — die Signatur aus §5.1.3.
Zieht man diese 6 Kontakte ab: **83 % grün / 1 % gelb / 14 % blau** gegen den Zielwert
88 / 1 / 10 aus §5.4. Die Logik war also korrekt, der Warmstart war das Problem.

**Konsequenz (umgesetzt):** `_reference_for` liefert `is_own`; solange gegen Gold gescort
wird, gibt `decide_feedback` **GRÜN** statt Richtung und statt AUS. Eine eigene Referenz ist
entweder das gefüllte Rolling-Fenster **oder** eine individuelle gespeicherte Baseline.

**Deshalb in der App immer das Athletenprofil wählen.** Mit individueller Baseline ist
`is_own` ab dem ersten Kontakt wahr, es gibt gar keinen Gold-Warmstart und die Richtung steht
sofort zur Verfügung. Ohne Profil (`global`) sind die ersten 6 Kontakte je Phase grün.

**Nebenwirkung im Blick behalten: Dauergrün bei kurzen Serien.** Bei ~18 Sprüngen und
12 Halten-Kontakten sind 6 davon Warmstart — der Validierungssatz kommt pro Serie auf
97–100 % grün. Das Übergabedokument warnt ausdrücklich vor „weder Dauergrün noch Dauerrot".
Bei kurzen Einheiten ohne Athletenprofil trägt die Ampel wenig Information.

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
python tools/test_refactor.py                      # 11 Tests (a–k), müssen alle grün sein
python tools/simulate_feedback.py <pfad|ordner> [--profile NAME] [--group-by SPALTE] [--live-check]
```

Die Testdatei muss die Fassung mit **a–k** sein. Eine ältere Fassung mit nur a–f ist im Umlauf;
ihr fehlen genau die Tests für diffI-Totband, Hysterese-Phase, Rolling-Referenz/MAD-Floor und
Gold-Warmstart — also für die zuletzt gebauten Teile. Bei 6 Tests: veraltete Datei.

**Verifiziert am Validierungssatz (10.08., nach dem Warmstart-Fix).** `data/hdts_knie_gesamt.xlsx`,
zwei Läufe — **die Zahlen stammen aus verschiedenen Läufen und dürfen nicht gemischt werden.**

| | `--group-by auto` (pro Serie) | `--group-by none` (eine Referenz über alle) | Ziel §5.4 |
|---|---|---|---|
| Phasen | **43 / 96** | 34 / 105 | 43 / 96 |
| Halten grün/gelb/blau/aus | **97 / 1 / 2 / 0** | 76 / 10 / 9 / 5 | 88 / 1 / 10 |
| trend Median | **+1.46**, 22 % neg. | +0.04, 47 % neg. | −0.04, 58 % neg. |

Nur der gruppierte Lauf ist methodisch zulässig (siehe „Fallstricke": Rolling-Referenz über
Athletengrenzen hinweg verfälscht alles) — und **er trifft §5.4 nicht**: 97 % Dauergrün. Der
ungruppierte Lauf liegt näher an §5.4, mittelt aber über fremde Körper; seine Nähe zum Zielwert
ist ein Artefakt, kein Beleg. Eine frühere Fassung dieses Absatzes zitierte die Phasenaufteilung
aus dem einen und die trend-Werte aus dem anderen Lauf — genau der Fehler, den dieses Dokument
verhindern soll.

**Ursache des Dauergrüns (Rechnung, kein Verdacht):** jede der 8 Serien hat ≥ 9 Halten-Kontakte,
also sind 8 × `MIN_ROLL` = **48 der 96** strukturell Warmstart-Grün. Von den restlichen 48, die
gegen die eigene Rolling-Referenz laufen, sind nur 3 nicht grün — das ist das Totband aus §7.1
(Kalibrierkurve dort: 0.50 → 12 % Feedback-Rate). Der Warmstart-Fix ist damit korrekt, aber er
verschiebt das Problem: auf kurzen Serien ohne Athletenprofil trägt die Ampel kaum Information.

**Das Quantil-Totband ist auf diesem Datensatz nicht messbar — das ist kein Mangel des Features,
sondern des Datensatzes.** Pro Serie braucht es erst `MIN_ROLL` = 6 Warmstart-Kontakte und dann
`DEADBAND_MIN_N` = 8 eigene, bis das Quantil greift: **14 Halten-Kontakte**. Nur `lydia` (19)
kommt darüber, also erreicht das Quantil **5 von 96** Halten-Kontakten (5 %). Der gruppierte Lauf
bewegt sich entsprechend nur von 3 % auf 4 % Richtungsanteil. **Diese Zahl nicht als Beleg gegen
das Feature lesen.**

Isoliert man die Mechanik auf einem durchgehenden Kontaktstrom (`--group-by none`, 105 Halten,
Quantil ab Kontakt 14 aktiv — methodisch **kein** Validierungswert, nur ein A/B der Logik):

| Totband | grün | gelb | blau | aus | Richtung | Band Median |
|---|---|---|---|---|---|---|
| fix 0.5 | 76 % | 10 % | 9 % | 5 % | 19 % | 0.50 |
| P70 | 76 % | 14 % | 9 % | **1 %** | 23 % | 0.48 |

Der Gewinn kommt **nicht** aus einem systematisch kleineren Band (Median 0.48 ≈ 0.50), sondern
aus der Anpassung je Kontakt. Bemerkenswert: „aus" fällt von 5 % auf 1 % — genau der Zustand,
den der Nutzer minimiert haben will, weil ein nicht leuchtendes Gerät wie ein defektes wirkt.

### Erster Lauf mit Athletenprofil (11.08.) — `louis_all.csv`

`louis_all.csv` ist **derselbe Datensatz** wie die §5.4-Zeile „Live-Test" (103 Sprünge, 1
Erwachsener) und war im Ordnerlauf die einzige Datei mit eigener Baseline (`[Profil: louis]`).
Damit ist es ein direkter Vorher/Nachher-Vergleich auf identischen Daten:

| Halten (58 Kontakte) | §5.4 vorher | nach dem Umbau |
|---|---|---|
| Licht an | 71 % | **75 %** |
| grün | 67 % | 66 % |
| gelb | **25 %** | **16 %** |
| blau | **0 %** | **9 %** |
| aus | 9 % | 10 % |

**Der einseitige Gelb-Überhang aus §5.1.3 ist beseitigt** — Blau von 0 auf 9 %, Gelb von 25 auf
16 %, bei praktisch unverändertem Grün- und Aus-Anteil. Richtungsrate 25 % gegen die ~30 %, auf
die P70 ausgelegt ist. `trend` Median +0.09 bei 44 % negativ (Ziel der Validierungsgruppe:
−0.04 / 58 %). Das Quantil war hier auf rund 50 der 58 Halten-Kontakte aktiv.

**Der Gesamtwert desselben Laufs (248 Sprünge, 8 Dateien) ist KEIN Kalibriermaßstab.** Sechs der
sieben Athleten liefen auf `[Profil: global]`, also mit Gold-Warmstart und 100 % Grün im Aufbau;
außerdem mischt er einen Erwachsenen (103 Sprünge, Zielwert 67/25/0/9) mit sechs Kindern
(Zielwert 88/1/10). Die Zeile `louis_all.csv` ist die aussagekräftige, nicht die GESAMT-Zeile.

**Nächster Schritt:** Baselines für die sechs Kinder anlegen, dann denselben Lauf wiederholen.
Erst dann ist die Kinder-Gruppe gegen §5.4 (88/1/10) messbar.

`--group-by` startet die Referenz je Athlet/Serie neu (Default `auto`: nimmt `Athlet` bzw.
`Serie`, falls die Spalte existiert). Der Loader erkennt zweizeilige Kopfzeilen und entfernt
Einheiten-Suffixe (`Peak_t [s]` → `Peak_t`). Liegen `X.csv` **und** `X_all.csv` nebeneinander,
wird `X.csv` übersprungen: das ist die auf `HG > 0.1` gefilterte Baseline-Teilmenge, also reine
Lade-Kontakte — sie doppelt zu zählen hat am 10.08. den Gesamtschnitt verfälscht.

`simulate_feedback.py` treibt die **echten Produktionspfade** offline an (kein Nachbau):
Phasenerkennung + Rolling-Referenz aus `JumpAnalyzer`, `compute_jump_score`, `decide_feedback`.
`--profile` steuert den Warmstart (Default `global` = Goldstandard; besser: der Athletenname,
damit nicht gegen Gold warmgestartet wird). `--live-check` schickt `.npz`-Rohsignale zusätzlich
durch `process()` in 48er-Blöcken; kleine Randabweichungen von 1–2 Sprüngen sind normal, weil
die inkrementelle Kontakterkennung anders segmentiert als die Offline-Variante.

Vor jeder Parameteränderung: erst die Datenlage klären (welche Dateien, welches Profil, pro
Serie getrennt?), dann rechnen. Die Zielwerte oben sind der Maßstab — mit Datensatz-Label.
