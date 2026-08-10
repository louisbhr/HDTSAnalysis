# HDTS – Übergabedokument Score-Validierung & Ampel-Logik

**Stand:** 06.08.2026 (Rev. 2 – `esp_client.py` geprüft, Abschnitt 4.1 ergänzt)
**Zweck:** Anker für die Weiterarbeit, auch in einer neuen Session. Enthält alle validierten Befunde mit Zahlen, den Stand der Umbaupläne, die offenen Entscheidungen und die Datenqualitäts-Fallstricke.

---

## 1. Projektziel (unverändert gültig)

HDTS gibt Kindern auf dem Trampolin über eine LED-Ampel **während der Flugphase** Feedback zum **letzten** Kontakt, damit sie es beim nächsten umsetzen können.

- Zwei Trainingsziele: **erst Höhe aufbauen, später Höhe halten.**
- Hardware: **gelb (links) / grün (mitte) / blau (rechts) / aus** – keine Firmware-Änderung geplant, kein Puls-Signal.
- Es ist eine **Arbeitslösung für die Praxis**, keine wissenschaftliche Arbeit. Kein Bedarf an Kapiteln, Publikationsformaten o. ä.
- Die **Kraftmessplatte bleibt das Instrument.** Die Kamera war ausschließlich Validierungswerkzeug und läuft im Betrieb nicht mit. Eine Kamera wäre langfristig eine Option, aber nicht der Zweck.
- **Entschieden:** Ein Kind, das niedriger als sonst springt, aber Höhe hält, bekommt **Halten-Feedback**.

---

## 2. Datenbasis

### 2.1 Validierungsdatensatz (`alles.xlsx` + Video + Kniewinkel)

139 Sprünge, **7 Personen / 8 Serien**. Reihenfolge in der zuletzt hochgeladenen `alles.xlsx`
(rekonstruiert über `video.xlsx`, Kontaktzeiten stimmen auf 1e-6 überein):

| Zeilen | Serie |
|---|---|
| 1–18 | maya |
| 19–37 | valeria |
| 38–57 | lydia |
| 58–69 | johanna1 |
| 70–87 | johanna2 |
| 88–105 | jonas |
| 106–121 | jannick |
| 122–139 | julian |

- **johanna1 und johanna2 sind dieselbe Person** (zwei Serien) → 7 Personen.
- **julian** war der frühere „Ausreißer" mit trend ≠ abs.
- Video: 240 fps, händisch ausgewertet, 1–2 Frames Unsicherheit.
- Kniewinkel: 114 Kurven, davon **103 verwertbar** (7 Athleten).

### 2.2 Live-Testdaten (Prototyp)

`louis_all.csv`: 103 Sprünge in 2 Sessions (68 + 35), aufgezeichnet 23.07.2026.
Session 1 lief **ohne Baseline** (Goldstandard, h_max = 4.5 m). Nach Session 1: Aufbau-Baseline n = 16 (individuell), Halten-Baseline n = 12 → **unter der Mindestgrenze, Gold-Fallback**.

### 2.3 Datenqualitäts-Fallstricke

- **`johanna1_neu.xlsx` hat eine defekte Zeitachse.** Werte laufen rückwärts (−14147 → −14427), Abstände entsprechen dem Doppelten der echten Zeitdifferenz aus `video.xlsx`. Die 11 Kurven wurden ausgeschlossen. Kurvenform sieht plausibel aus – bei vorhandenen Rohvideos rekonstruierbar (+12 Sprünge).
- Die zuletzt hochgeladene `alles.xlsx` war noch die **alte Version** ohne Leerzeilen zwischen den Blöcken.
- Kombinierter Export liegt vor: **`hdts_knie_gesamt.xlsx`** (139 Zeilen, HDTS + Video + 17 Kniewinkel-Spalten, Legendenblatt).

---

## 3. Validierte Befunde

Alle Korrelationen **within-Person** (personenzentriert), sofern nicht anders angegeben. Mixed Models mit Person als Random Effect.

### 3.1 Der Score misst Leistung ✔

- `abs_score` ↔ Sprunghöhe: **within r = −0.71**, **between (Serienmittel) r = −0.98**
- Konsistent über alle 8 Serien (Einzelwerte −0.16 bis −0.90)
- **Interpretation:** Näher am Referenzmuster → höherer Sprung. Gilt auch innerhalb einer Person.

### 3.2 Das Aufbau-Paradox ✔ (wichtigster inhaltlicher Befund)

- `abs_score` ↔ HG: **within r = +0.51**
- MixedLM `HG ~ abs_score + Height`: β_abs = **+0.18 (p = 0.005)** – bleibt auch bei kontrollierter Höhe
- HG ↔ Kontaktzeit: within r = +0.52
- **Kein Artefakt:** Simulation reiner Mean-Reversion ergäbe **r ≈ −0.13**, beobachtet ist +0.51.
- **Interpretation:** Der Kontakt, der Höhe erzeugt, ist der lange, weiche „Lade-Kontakt", der vom Profi-Muster abweicht. → Begründet die gesamte Zwei-Phasen-Architektur.

### 3.3 Kontakterkennung ist valide ✔

- Kontaktzeit Kraftplatte (`Peak_t / Peak_Prct × 100`) vs. Video: **r = 0.95**
- Systematischer Versatz **−60 ms** (SD 8.7 ms), erklärbar durch die 5 %-Peak-Schwelle
- Versatz ist konstant → stört nicht.

### 3.4 Nadir-Timing ist invariant (kein Diskriminator) ✔

- `nadir_PrctV`: within-SD **1.11 Prozentpunkte**
- 1 Frame bei 240 fps = **1.14 pp** der Kontaktzeit; realistische Messunsicherheit ±2.5–3 pp
- **Die gesamte Variation liegt unter der Messauflösung.** Nadir sitzt konstant bei ~47–48 % der Kontaktzeit.
- → Als Validierungskriterium streichen, als Konstanz-Befund berichten.

### 3.5 Kniewinkel: was er kann und was nicht ✔

**Geometrie auf dem Trampolin (Mediane):**
Kontakt 148° → Minimum 147° → Absprung 172°. **Beugung nur 1–2°**, Streckung 24°.
Die Amortisation passiert im Tuch, nicht im Knie.

**Was der Kniewinkel liefert:**

| Befund | Wert |
|---|---|
| Kniestreckung (`ext_total`) ↔ HG | **within r = +0.62** |
| MixedLM `HG ~ ext_total + Height` | β = **+0.0070 m/°** (p = 1.4e-10) ≈ **7 cm je 10°** |
| Bleibt bei Kontrolle für abs_score | β = 0.0071 (p = 3.6e-11) – **unabhängig vom Score** |
| `v_max` zusätzlich zu ext_total | β = +0.00074 je °/s (p = 0.002) ≈ 7 cm je 100 °/s |

**Was der Kniewinkel NICHT liefert:**

- **Keine Konvergenzvalidierung des Peak-Timings.** Knie-Minimum bei 20 % der Kontaktzeit, Kraft-Peak bei 48 %, Körper-Nadir bei 47 %, max. Streckgeschwindigkeit bei 81 %. Within-Korrelationen Knie-Timing ↔ Kraft-Timing: **r ≈ 0** (alle p > 0.4). Die Gesamtkorrelationen (+0.33 bis +0.43) sind reine Between-Athlet-Effekte.
- **Streckungsbeginn trägt keine eigenständige Information.** Über 7 Definitionen getestet (Knie-Minimum, 10/25/50 % Amplitude, 20/50 % v_max, t_vmax), jeweils absolut und in % der Kontaktzeit. Roh: früher strecken → mehr HG (r = −0.22 bis −0.29). **Aber:** stark an ext_total gekoppelt (r = −0.57); im Modell mit ext_total fällt der Effekt auf p = 0.13.
- **Kein „zu früh strecken" nachweisbar.** Quadratische Terme n.s. (p = 0.33–0.64), Extremgruppen-Vergleich zeigt monotonen Trend ohne Umschlagen. **Aber:** Die Stichprobe enthält vermutlich gar keine solchen Fälle (alle Peak_Prct zwischen 30–58 %). Physiologisch muss eine Grenze existieren → Sicherung nötig (siehe 5.3).

### 3.6 Was der Score inhaltlich misst: **Landesteifigkeit** ✔

| Kennzahl | ↔ trend_score | p |
|---|---|---|
| Kniewinkel bei Kontakt | **−0.31** | 0.001 |
| Kniewinkel Minimum | **−0.39** | 0.0001 |
| Kniewinkel bei Absprung | −0.28 | 0.005 |
| Streckung gesamt | +0.22 | 0.024 |
| **Zeitpunkt Streckungsbeginn** | **−0.02** | 0.83 |
| **Zeitpunkt v_max** | −0.09 | 0.38 |

Zusätzlich: `ang_contact` ↔ Höhe **+0.46**, ↔ Kontaktzeit **−0.35**, ↔ abs_score **−0.31**.

**Der Score reagiert auf Winkel, nicht auf Zeitpunkte.**
„Ampel zeigt früher" bedeutet in Wirklichkeit: **gestreckter / steifer landen** – nicht „früher strecken".
Mechanisch schlüssig: gestrecktes Bein = steifer = Tuch baut Kraft schneller auf = Peak wandert nach vorn.

**Konsequenz:** Steif = kleiner Score = hoch springen und halten. Weich mit viel Streckung = großer Score = Höhe aufbauen. → Bestätigt die Zwei-Phasen-Architektur von der biomechanischen Seite.

### 3.7 Test-Retest (Johanna, 2 Serien) ✔

- Feature-Mediane verschieben sich zwischen Serien um **~1 MAD-SD**
- Zwischen-Athleten-Streuung ist **2.6× bis 10.4×** größer als die mittlere within-Streuung
- ICC-artige Varianzanteile: Peak_t 0.87, postSlope 0.87, Symmetry 0.70, Peak_Prct 0.68, Explosiv 0.57, preSlope 0.55, abs_score 0.53
- **Interpretation:** Das individuelle Profil ist über Sessions grundsätzlich stabil, aber **nicht konstant genug für eine wochenalte Baseline als alleinige Referenz** → Argument für die Rolling-Referenz.

---

## 4. Strukturelle Probleme im Code (aufgedeckt, teils behoben)

| Problem | Status |
|---|---|
| `timing` == exakt `100 × Peak_t` → perfekte Kollinearität, doppeltes Gewicht | Entfernen geplant |
| `Peak` in `var_names`, aber nie in `current_features` → totes Feature | Entfernen geplant |
| MAD ohne Faktor 1.4826 → individuelle z-Werte ~1.48× überhöht, andere Skala als Gold | `MAD_CONSISTENCY` eingeführt |
| Schwellen 2.5 / 4.5 feuern nie (abs_score empirisch 1.29–2.63, Median 1.60) | Neu auf 1.4 / 1.9 |
| Nur ~14 % der Gold-Importance werden genutzt (diffI, diffI_norm, diffI_symmetry, Contact_t = ~73 % liegen brach) | Bewusst so (Zirkularität) |
| **HG > 0.1-Qualitätsfilter selektiert systematisch Lade-Sprünge** → Baseline lernt Aufbau-Stil als Norm für Halten | Zwei Referenzsätze eingeführt |
| Importance-Regression `|corr(Feature, HG)|` lernt Aufbau-Prädiktoren | Offen, ggf. auf Height umstellen |
| Protokollnamen `EARLY`/`LATE` sind in der Firmware verankert | Unkritisch – Wording-Änderung betrifft nur die GUI |
| **Gold-Fallback für „halten"** widerspricht der eigenen Analyse (Profi = fremder Körper → Dauersignal) | Soll durch Rolling-Referenz ersetzt werden |
| Individuelle MADs aus n = 16 absurd eng (Peak_Prct MAD 0.47 pp vs. GoldStd 6.0; Peak_t MAD 4.4 ms ≈ Messauflösung) | MAD-Floor geplant |
| **Text und LED laufen über zwei getrennte Codepfade** (`jump_analyzer` vs. `esp_client.classify_ampel`) | **Geprüft (06.08.):** Halten konsistent, Aufbau divergiert – siehe 4.1 |
| `AUFBAU_DIFFI_THRESHOLD` doppelt definiert (`jump_analyzer` = 0.0 vs. `classify_ampel` hardcodiert `> 0`) | **Offen** – muss zentralisiert werden |

### 4.1 Text vs. LED – Abgleich `jump_analyzer.process()` ↔ `esp_client.classify_ampel()`

**Geprüft am 06.08.2026.** Entwarnung teilweise: Die **Schwellwerte sind gemeinsam** (`classify_ampel` importiert `STEP_THRESHOLD_*`, `DEADBAND_TREND`, `CONSISTENCY_GATE` aus `importance_utils`). Dupliziert ist nur die **Entscheidungslogik**.

**Phase „halten": vollständig konsistent ✔**
Gleiche Reihenfolge (Totband → Gate → Richtung), gleiche Schwellen, gleiche Stufen.

**Phase „aufbau": divergiert ✘**

| Situation | Text (`jump_analyzer`) | LED (`classify_ampel`) |
|---|---|---|
| `diffI` = NaN | „erster Sprung" | OFF ✔ |
| `diffI` > 0 | „Höhe kommt" | GOOD ✔ |
| `diffI` ≤ 0, Aufbau-Baseline da, \|trend\| ≥ Totband | **„mehr Druck ins Tuch"** | **GELB / BLAU** ✘ |
| `diffI` ≤ 0, Aufbau-Baseline da, \|trend\| < Totband | „mehr Druck ins Tuch" | OFF ✘ |
| `diffI` ≤ 0, keine Aufbau-Baseline | „mehr Druck ins Tuch" | OFF ✘ |

**Der Text kennt im Aufbau überhaupt keine Richtungsansage** – er kann nur „Höhe kommt" oder „mehr Druck ins Tuch". Die LED zeigt dort aber Richtungslichter, sobald eine individuelle Aufbau-Baseline existiert. Ein Kind sieht also Blau, während im Log „mehr Druck ins Tuch" steht.

**Korrekt verdrahtet ist dagegen:**
`aufbau_ok = (self.mode_sources.get("aufbau") == "individuelle Baseline")` → die Fallback-Regel „kein Gold als Aufbau-Referenz" greift auf der LED-Seite wie geplant.

**Weitere Feststellungen aus `esp_client.py`:**

- `AMPEL_INVERT_DIRECTION = False` – Flag existiert, falls die Richtung auf der Hardware vertauscht wirkt.
- Historie im Docstring: Eine **frühere Version zeigte den Fehler statt der Anweisung** (trend > 0 → blau). Jetzt korrekt: trend > 0 → **gelb = Anweisung**. Beim Testen darauf achten, dass keine alte Firmware/Version gemischt wird.
- Protokoll: `SHOW EARLY <1..3>` / `SHOW LATE <1..3>` / `SHOW GOOD` / `OFF`. Level 0 bei GOOD und OFF, 1–3 bei EARLY/LATE. Die Namen `EARLY`/`LATE` sind firmwareseitig fix – die geplante Umbenennung („steifer/weicher landen") betrifft **nur die GUI-Texte**, keine Firmware.
- Zwei Transportwege: USB-Serial (braucht `pyserial`) und WLAN/TCP (`192.168.4.1:3333`, AP „HDTS-Ampel"). Modul ist auch ohne `pyserial` importierbar.

**Empfehlung:** Beim Umbau die Entscheidung **einmal** treffen (eine Funktion, die `(led_direction, led_level, text)` zurückgibt) und daraus Text *und* LED ableiten. Die Aufbau-Richtungsansage aus 5.2 gilt dann für beide.

---

## 5. Ampel-Logik: Ist-Zustand und Zielzustand

### 5.1 Was im Live-Test schiefging (diagnostiziert)

Drei Beobachtungen, eine Ursachenkette:

1. **Phasen flattern:** Feste Schwelle `h_prev / h_max ≥ 0.9`. Reisehöhe lag genau auf der Schwelle → 7 Wechsel bei 35 Sprüngen. Zusätzlich konzeptionell falsch: `h_max` ist All-Time-P95, an schwachen Tagen nie „halten".
2. **Nicht bei jedem Sprung Licht:** `diffI > 0` ist bei stabilem Springen ein Münzwurf (Differenz oszilliert um null, negative Autokorrelation). 62 von 103 Sprüngen „aus".
3. **Immer „Alles gut" oder „sehr deutlich früher":** Halten-Referenz war der **Goldstandard** (Fallback, weil nur 12 Halten-Sprünge). Gegen Gold liegt trend konstant bei +1.2 bis +2.6 σ, Konsistenz ≈ 1 → **100 % „früher treten"**, in 139 von 139 Fällen positiv.

### 5.2 Zielzustand der Ausgabe-Logik (beschlossen)

Beide Phasen nutzen **dieselben vier Ausgabezustände**, nur mit phasenabhängiger Referenz und Grün-Definition.

```
Phase HALTEN – Score gegen Halten-Referenz:
    |trend| < DEADBAND                     -> GRÜN  (Timing stabil)
    trend > +DEADBAND und |trend|/abs>GATE -> GELB  (steifer landen)
    trend < -DEADBAND und |trend|/abs>GATE -> BLAU  (weicher landen)
    Gate verletzt                          -> AUS   (uneinheitlich)

Phase AUFBAU – Score gegen Aufbau-Referenz:
    diffI > DIFFI_DEADBAND                 -> GRÜN  (Erfolg schlägt Muster:
                                              Höhe gewonnen -> immer grün,
                                              auch bei Timing-Abweichung)
    diffI <= Schwelle und trend < -DEADBAND -> BLAU
    diffI <= Schwelle und trend > +DEADBAND -> GELB
    diffI <= Schwelle und |trend| < DEADBAND-> AUS
```

**Begründung:** Die Aufbau-Baseline besteht aus Kontakten, die Höhe erzeugt haben – gegen *diese* Referenz ist früher/später auch im Aufbau sinnvoll. Nur die Bewertung gegen Halten-/Profi-Referenz wäre im Aufbau falsch. Vorteil: Die Lichter bedeuten in beiden Phasen dasselbe (didaktisch wichtig für Kinder).

**Fallback-Regel:** Bei < 15 Aufbau-Sprüngen **kein Gold-Fallback** (Gold beschreibt Steady-State-Kontakte, als Aufbau-Referenz genau falsch). Stattdessen im Aufbau nur das diffI-Kriterium (GRÜN bei diffI > Schwelle, sonst AUS), Richtungslichter erst wenn Aufbau-Baseline steht.

### 5.3 Vier geplante Änderungen an der Auswertung

| # | Änderung | Begründung |
|---|---|---|
| 1 | **Phase aus HG-Dynamik** statt aus `h_max`: Einstieg Aufbau bei `HG[i-1] > 0.15` **oder** Mittel der letzten 3 HG > 0.08; Ausstieg nach 2 ruhigen Sprüngen (asymmetrische Hysterese) | Trifft die Coaching-Realität; kein Flattern; funktioniert auf jeder Höhe |
| 2 | **Rolling-Referenz** aus den letzten **12 Kontakten** der laufenden Session (ab 6 Kontakten; davor Warmstart aus gespeicherter Baseline) | Tagesformrobust, kein systematischer Schiefstand, löst den Fallback auf |
| 3 | **MAD-Floor:** `deviation = max(MAD, 0.30 × GoldStd)` | Kappt z-Explosion aus kleinen homogenen Teilmengen |
| 4 | **diffI-Totband:** GRÜN erst ab `diffI > 22` (kalibriert: robuste SD von diffI bei stabilen Sprüngen = 21.8, Median −4.8) | Verhindert Münzwurf-Verhalten |

**Zusätzlich beschlossen:**

- **Wording ändern:** „früher/später treten" ist irreführend – es suggeriert eine zeitliche Handlung, die es so nicht gibt. Besser: **Gelb = „Beine strecken / steifer landen", Blau = „weicher landen / mehr nachgeben"**. (Endgültige Formulierung offen, siehe 7.)
- **Sicherung gegen „immer steifer":** Rolling-Referenz wandert mit (verhindert Ziehen ins Extreme) **plus** absoluter Deckel: bei `Peak_Prct < ~25 %` kein GELB mehr, sondern GRÜN oder AUS. Reine Vorsichtsmaßnahme, Schwelle bisher nie erreicht.

### 5.4 Simulationsergebnisse der neuen Logik

**Auf den Live-Testdaten (103 Sprünge, 1 Erwachsener, bewusst chaotisch):**

| | alte Logik | neue Logik |
|---|---|---|
| Licht an | ~40 % | **71 %** |
| Halten: stabil / gelb / blau / uneinheitlich | 0 / 100 / 0 / 0 % | **67 / 25 / 0 / 9 %** |
| Aufbau: Höhe kommt / Richtung / kein Signal | – | 16 / 5 / 23 Sprünge |

**Auf dem Validierungsdatensatz (139 Sprünge, 8 Serien):**

- Phasen: 43 Aufbau / 96 Halten
- Halten mit Rolling-Referenz (n = 86 nach Warmstart): **88 % grün, 10 % blau, 1 % gelb**
- trend Median **−0.04**, **58 % negativ** (vorher: 0 %) → Richtungssignal ist endlich symmetrisch
- abs Median **0.27**

---

## 6. Aktuelle Parameter (Vorschlagswerte)

```python
# importance_utils.py
MAD_CONSISTENCY        = 1.4826   # MAD -> Sigma-Schätzer
TARGET_IMPORTANCE_SUM  = 1.0
STEP_THRESHOLD_MEDIUM  = 1.4      # "deutlich"
STEP_THRESHOLD_STRONG  = 1.9      # "sehr deutlich"
DEADBAND_TREND         = 0.5      # !! siehe offener Punkt 7.1
CONSISTENCY_GATE       = 0.6

# neu geplant
ROLL_N                 = 12       # Fenster Rolling-Referenz
MIN_ROLL               = 6        # ab hier Rolling statt Warmstart
MAD_FLOOR_FACTOR       = 0.30     # x GoldStd
HG_AUFBAU_ENTRY        = 0.15     # Sofort-Einstieg Aufbau
HG_AUFBAU_MEAN3        = 0.08     # Mittel letzte 3 HG
HYSTERESE_EXIT         = 2        # ruhige Sprünge bis "halten"
DIFFI_DEADBAND         = 22.0     # ersetzt AUFBAU_DIFFI_THRESHOLD=0.0 an BEIDEN Stellen
PEAK_PRCT_SAFETY_MIN   = 25.0     # darunter kein GELB

# esp_client.py (Ist-Zustand)
AMPEL_INVERT_DIRECTION = False    # Richtung auf der Hardware ggf. tauschen
DEFAULT_WIFI_HOST      = "192.168.4.1"
DEFAULT_WIFI_PORT      = 3333

# baseline_manager.py
MIN_JUMPS_PER_MODE     = 15
H_MAX_PERCENTILE       = 95       # robust statt max()
HALTEN_HEIGHT_FRACTION = 0.9
HG_QUALITY_THRESHOLD   = 0.1      # Aufbau-Referenz
```

**Score-Features (nach Bereinigung, 6 Stück):**
`Peak_t, Peak_Prct, Explosiv, preSlope, postSlope, Symmetry`
Richtungen: `Peak_t +1, Peak_Prct +1, Symmetry +1, postSlope +1, preSlope −1, Explosiv −1`
(`Peak` und `timing` weiter berechnen und loggen, aber nicht scoren.)

---

## 7. Offene Punkte

### 7.1 Totband muss quantilbasiert werden (wichtigster offener Punkt)

Ein **fester** Wert 0.5 verhält sich je nach Athlet völlig anders:

| Datensatz | Feedback-Rate bei Totband 0.5 |
|---|---|
| Live-Test (1 Erwachsener, chaotisch) | ~33 % |
| Validierungsgruppe (8 Serien, konsistent) | **12 %** (88 % Dauergrün) |

Kalibrierungskurve auf der Validierungsgruppe: Totband 0.25 → 34 %, 0.30 → 30 %, 0.40 → 20 %, 0.50 → 12 %.

**Vorschlag:** Totband als **Quantil der eigenen jüngsten trend-Verteilung** (z. B. P70 der letzten 12–20 Kontakte) statt als feste Zahl. Kalibriert sich pro Kind selbst. **Noch nicht entschieden.**

### 7.2 Übergangssprünge am Ende einer Aufbau-Episode

23 von 44 Aufbau-Sprüngen bekamen „kein Signal" – fast alle am Ende einer Aufbau-Episode (Phase hängt 2–3 Sprünge nach, diffI im Totband). Optionen:

- **(a)** So lassen – „aus" ist im Übergang ehrlich, 71 % Leuchtquote ist okay
- **(b)** Ausstieg aggressiver (Mittel der letzten 2 HG statt 3) – weniger Nachhängen, mehr Wechsel
- **(c)** Diese Sprünge der Halten-Logik geben, sobald diffI im Totband liegt („wer nicht mehr gewinnt, wird auf Stabilität bewertet") – **Empfehlung**, verschiebt aber die Phasendefinition

**Nicht entschieden.**

### 7.3 Verhalten bei absichtlichem Höhenabbau

Gelb-Häufung sitzt fast komplett in Abstiegen (z. B. Höhe fällt 2.5 → 1.2 m → „sehr deutlich früher"). Frage: Ist das der richtige Cue, oder soll die Ampel bei kontrolliertem Abbremsen **stumm** sein (Erkennung: HG deutlich negativ über 2 Sprünge)? Sonst bestraft sie das kontrollierte Abbremsen, das Trainer bewusst wollen. **Nicht entschieden.**

### 7.4 Wording für die Ausgabe

„steifer landen" / „weicher landen" ist aus den Daten abgeleitet, aber **keine Trainingssprache**. Welche Worte versteht ein Kind auf dem Trampolin? **Offen.**

### 7.5 Code-Zustand

**Geprüft (06.08.2026):** `esp_client.py` liegt vor und ist analysiert → siehe 4.1. Text/LED sind im Halten konsistent, im Aufbau nicht.

**Weiterhin unklar:**

- Zuletzt gesehene `jump_analyzer.py` / `main.py` / `importance_utils.py` sind die Version **vor** dem Rolling-Referenz-Umbau (Stand 29.07.2026), `baseline_manager.py` / `profiler.py` vom 23.07.2026. Ob seither weitergebaut wurde: unbestätigt.
- **Zusammenführung von Text- und LED-Entscheidung** ist noch nicht umgesetzt (4.1).
- `AUFBAU_DIFFI_THRESHOLD` an zwei Stellen definiert – muss vor der Einführung des diffI-Totbands (22.0) zentralisiert werden.

### 7.6 Importance-Ziel

Regression `|corr(Feature, HG)|` lernt Aufbau-Prädiktoren. Für den Halten-Modus wäre `|corr(Feature, Height)|` passender. Zwei Gewichtssätze je Phase wären konsequent. **Nicht umgesetzt.**

---

## 8. Nächste Schritte

1. **Text- und LED-Pfad zu einer Entscheidung zusammenführen** (4.1) – die Aufbau-Divergenz ist der einzige bestätigte Logikfehler; dabei `AUFBAU_DIFFI_THRESHOLD` zentralisieren. Aktuellen Repo-Stand der übrigen Dateien gegenprüfen.
2. Offene Punkte 7.1–7.4 entscheiden
3. Umbau 5.3 umsetzen (Phase aus HG-Dynamik, Rolling-Referenz, MAD-Floor, diffI-Totband, Wording)
4. Testskript: alle 8 Ampel-Zweige (4 pro Phase) synthetisch durchschalten, plus Aufbau-Fallback ohne Baseline
5. **Erste echte Kinder-Session** aufzeichnen → Ampelverteilung prüfen (weder Dauergrün noch Dauerrot), Parameter nachjustieren
6. Optional: **Interventionsstudie.** Alles bisherige ist Beobachtung; ob das Lichtsignal *kausal* wirkt, zeigt nur ein Vergleich von Serien mit und ohne Feedback. Praktikabel: alternierende Serien pro Kind (randomisierte Reihenfolge), Zielgrößen „Sprünge bis 90 % H_Max" (Aufbau-Effizienz) und SD(Height) im Halten. Mit ~18 Sprüngen pro Serie, 4–6 Serien pro Kind und 6–10 Kindern hat das Power.

---

## 9. Vorhandene Artefakte

| Datei | Inhalt |
|---|---|
| `hdts_knie_gesamt.xlsx` | 139 Sprünge: HDTS-Features, Scores, Videodaten, 17 Kniewinkel-Spalten, Legendenblatt |
| `1_validierung_uebersicht.png` | 6 Panels: Score↔Höhe, Aufbau-Paradox, Kontaktzeit-Konvergenz, Kniestreckung↔HG, Landesteifigkeit, Timing-Histogramm |
| `2_ampel_konsequenzen.png` | 4 Panels: Vorher/Nachher, trend-Verteilung, Totband-Kalibrierung, Phasenerkennung |

**Hinweis zur Qualität:** In `2_ampel_konsequenzen.png` sind die Balkenwerte in **Panel A hartcodiert** (die Simulationsdatei war zum Zeitpunkt der Erstellung nicht mehr verfügbar); die Kategorie „Richtung beidseitig?" ist eher Illustration als Messgröße. **Panel A nicht als Beleg verwenden.** Die übrigen 9 Panels sind aus den Rohdaten neu gerechnet.
