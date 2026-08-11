# HDTS – Übergabe an die nächste Session (Stand 11.08.2026)

**Zweck dieses Dokuments.** `CLAUDE.md` beschreibt den *Zustand* des Projekts und wird zu
Sessionbeginn automatisch gelesen. Dieses Dokument beschreibt, was in der Session vom 11.08.
*passiert* ist, was davon belegt ist und was nicht — damit eine neue Session nicht aus
Zusammenfassungen rekonstruieren muss.

**Verhältnis zu `docs/HDTS_Uebergabe.md`:** Jenes Dokument (Stand 06.08.) bleibt die
autoritative Quelle für Befunde und die §-Nummerierung. Dieses hier ergänzt es, ersetzt es
nicht. Wo unten „§7.1" o. ä. steht, ist immer das 06.08.-Dokument gemeint.

---

## 1. Stand

| | |
|---|---|
| Branch | `claude/hdts-validation-fixes-iounkz` |
| Commit | `94370bc` (plus ein Nachtrag, siehe Abschnitt 5) |
| Tests | `python tools/test_refactor.py` → **12 Tests (a–l), alle grün** |
| Produktionscode geändert | `importance_utils.py`, `esp_client.py`, `jump_analyzer.py`, `baseline_manager.py` |
| Werkzeuge | `tools/simulate_feedback.py`, `tools/test_refactor.py`, `tools/build_baselines.py` (neu) |

---

## 2. Was in dieser Session gebaut wurde

### 2.1 Gold-Warmstart zeigt GRÜN (`8c5545f`)
`decide_feedback` bekam `reference_is_own`. Solange gegen den Goldstandard gescort wird — also
bevor das Rolling-Fenster gefüllt ist und ohne individuelle Baseline — gibt es **keine
Richtung und kein AUS, sondern GRÜN**. Anlass: in 5 von 7 Athletendateien standen im Halten
exakt `MIN_ROLL` = 6 GELB-Sprünge, also genau die Kontakte vor dem Füllen des Fensters.

### 2.2 Quantil-Totband, §7.1 (`387a013`)
`importance_utils.effective_deadband()` bildet das Totband als **P70 der letzten 20 eigenen
`|trend|`-Werte**, je Phase getrennt, ab 8 Werten, begrenzt auf [0.25, 1.20]. Bei P70 liegen
per Konstruktion ~30 % der Kontakte darüber — die Feedback-Rate ist damit *eingestellt* statt
vom Athleten abhängig.

Zwei Entwurfsentscheidungen, die getroffen wurden und begründet sind:
- `decide_feedback` bleibt **zustandslos** und bekommt das Band als Parameter `deadband=`.
  Das Gedächtnis (`_trend_hist`) liegt im Analyzer, wo Rolling-Fenster und Phase schon leben.
  Ohne Parameter gilt weiter der Fixwert → alle Altaufrufer laufen unverändert.
- Ins Fenster kommen **nur Kontakte mit eigener Referenz**. Gold-Werte liegen konstant bei
  +1.2 bis +2.6 σ und würden das Quantil aufblasen.

Zurückschalten: einzig `DEADBAND_MODE = "fix"` in `importance_utils.py`.

### 2.3 Kein Gold-Leak im Halten-Fallback (`415bc59`) — der wichtigste Fund
`baseline_manager` schrieb bei unter 15 Halten-Sprüngen Gold-Werte **unter dem Athletennamen**
in die Baseline-CSV. `jump_analyzer.load_profile` erkennt „individuelle Baseline" aber allein
daran, *ob für den Modus überhaupt eine Zeile existiert* — der Gold-Fallback war von einer
echten eigenen Referenz nicht zu unterscheiden. Folge: `is_own=True`, volles Richtungsfeedback
gegen einen **fremden Körper**. Das ist exakt §5.1.3, nur über einen zweiten Pfad, den 2.1
nicht abdeckte.

Behoben durch dieselbe Regel wie im Aufbau: unter `MIN_JUMPS_PER_MODE` **keine Zeile
speichern**. `_gold_fallback_row_values()` hatte danach keinen Aufrufer mehr und ist entfernt.
Test l) deckt den Fund End-to-End ab.

### 2.4 Werkzeuge
- `--profile auto` (`7addc19`): wählt je Datei/Serie die passende Baseline, sonst `global`.
  Vorher hätte ein einzelnes `--profile` über einen Ordner jeden Athleten gegen **einen**
  Körper warmgestartet.
- `--recurse` nicht mehr Default + `--exclude` (`716bb04`): siehe Abschnitt 4.
- `_baseline.csv` wird nicht mehr als Sprungdatei gelesen (`e08cd0e`).
- `tools/build_baselines.py` (`960a15e`): baut Baselines für alle Athleten aus den
  `*_all.csv`. Reiner Wrapper um `baseline_manager.update_athlete_baseline()`.

---

## 3. Was gemessen wurde — mit Datensatz-Etikett

**Regel: keine Zahl ohne Datensatz nennen.** Die drei Datensätze haben verschiedene Zielwerte.

### 3.1 Kinder-Gruppe, live, mit eigenen Baselines — **Zielwert getroffen**

100 Halten-Kontakte, 6 Kinder, exakt aus den Kontaktzahlen aufsummiert (nicht aus gerundeten
Prozenten):

| | erreicht | Ziel §5.4 |
|---|---|---|
| grün | **88 %** | 88 % |
| gelb | **2 %** | 1 % |
| blau | **9 %** | 10 % |
| aus | **1 %** | – |

Wirkung des Fixes aus 2.3 an genau der Datei, die er betraf: das einzige Kind mit unter 15
Halten-Sprüngen wechselte von 45 % grün / 55 % gelb auf 82 / 9 / 9; die anderen fünf blieben
unverändert.

**Einschränkung:** Der Zielwert stammt aus dem kameravalidierten `data/hdts_knie_gesamt.xlsx`,
dieser Lauf aus dem Live-Werkzeug ohne Kamera. Die Vornamen decken sich weitgehend — vermutlich
dieselbe Population an einem anderen Tag, **kein unabhängiger Datensatz**.

### 3.2 Erwachsener (`louis_all.csv`, 103 Sprünge) — Vorher/Nachher auf identischen Daten

Derselbe Datensatz wie die §5.4-Zeile „Live-Test":

| Halten (58 Kontakte) | §5.4 vorher | jetzt |
|---|---|---|
| Licht an | 71 % | 75 % |
| grün | 67 % | 66 % |
| gelb | **25 %** | **16 %** |
| blau | **0 %** | **9 %** |
| aus | 9 % | 10 % |

Der einseitige Gelb-Überhang aus §5.1.3 ist beseitigt.

### 3.3 Validierungssatz (`data/hdts_knie_gesamt.xlsx`) — trifft §5.4 **nicht**

Der methodisch korrekte Lauf (`--group-by auto`, pro Serie) liefert 96 % Dauergrün statt
88/1/10. Ursache ist gerechnet, nicht vermutet: 8 Serien × `MIN_ROLL` = 48 der 96
Halten-Kontakte sind strukturell Warmstart. Ohne Athletenprofil trägt die Ampel auf kurzen
Serien wenig Information — das ist die bekannte Nebenwirkung von 2.1.

Das Quantil-Totband erreicht auf diesem Datensatz nur **5 von 96** Kontakten (pro Serie braucht
es 6 Warmstart + 8 eigene = 14 Halten-Kontakte). **Diese Zahl nicht als Beleg gegen das Feature
lesen.**

---

## 4. Fallstricke, die in dieser Session real zugeschlagen haben

Alle drei stehen auch in `CLAUDE.md`, hier mit dem Vorfall dazu:

1. **`.npz` und `_all.csv` sind dieselben Sprünge.** Ein rekursiver Ordnerlauf meldete **707**
   Sprünge aus 37 Dateien. Die Summe geht glatt auf: 345 aus den `_all.csv` + 362 aus den
   `.npz` = 707. Beweis: eine `_all.csv` und die zugehörige Session-`.npz` lieferten bitgenau
   denselben `trend_median`. Deshalb ist `--recurse` nicht mehr Default.
2. **Testdateien verfälschen die Kennzahlen.** `test*`, `video_test*`,
   `master_session_daten*` gehören mit `--exclude` raus.
3. **Zahlen aus verschiedenen Läufen nicht mischen.** Eine frühere Fassung von `CLAUDE.md`
   zitierte die Phasenaufteilung aus dem gruppierten und die trend-Werte aus dem ungruppierten
   Lauf — korrigiert in `8b49d08`.

Der Standardaufruf, der alle drei vermeidet:

```bash
python tools/simulate_feedback.py athleten_daten --profile auto \
  --exclude "test*" --exclude "video_test*" --exclude "master_session_daten*"
```

---

## 5. Offene Punkte

### Datenschutz — **erledigen, bevor etwas anderes passiert**
- `kennzahlen.csv` mit Klarnamen war kurzzeitig im öffentlichen Repo. Der Commit wurde
  bereinigt und die Datei ist jetzt gitignored, aber **der alte Commit bleibt über seine SHA
  erreichbar**, bis GitHub ihn wegräumt.
- Danach wurden von mir versehentlich **Vor- und Nachnamen von Kindern in `CLAUDE.md` und in
  drei Codekommentaren** eingeführt (Commits `8b49d08`, `415bc59`, `94370bc`). Das ist im
  Arbeitsbaum korrigiert (Nachtrag-Commit, siehe unten), aber ebenfalls in der gepushten
  Historie.
- **Empfehlung: Repo auf privat stellen.** Das erledigt beide Punkte in einem Schritt und ist
  bei Bewegungsdaten von Minderjährigen ohnehin die naheliegendere Einstellung. Wurde
  vorgeschlagen, ist **nicht entschieden**.

### Entschieden, aber nicht gebaut
- **`PEAK_PRCT_SAFETY_MIN = 25.0`** (§5.3/§6): unter ~25 % `Peak_Prct` kein GELB mehr. Klein,
  unstrittig, empirisch nie erreicht.

### Offen — nicht eigenmächtig entscheiden
- **§7.2 Übergangssprünge.** Praktisch relevant: im Aufbau stehen 22 % „aus", fast vollständig
  aus `louis` (44 %). Empfehlung des Dokuments ist Variante (c).
- **§7.3 Kontrollierter Höhenabbau** — Ampel beim absichtlichen Abbremsen stumm?
- **§7.4 Wording.** Das Dokument argumentiert gegen „früher/später treten". **Der Nutzer hat
  entschieden, das alte Wording zu behalten — diese Entscheidung steht und überstimmt das
  Dokument.**
- **§7.6 Importance je Phase.**

### Noch nie getestet
**Eine echte Live-Session** mit Kraftmessplatte, ESP32-Ampel und Kind auf dem Trampolin. Alles
oben ist offline gegen aufgezeichnete Sprünge gerechnet. Beim ersten Lauf achten auf:
Athletenprofil auswählen (nicht `global`), Phasenwechsel-Verhalten, und das Protokoll
mitlaufen lassen.

---

## 6. Arbeitsweise mit dem Cloud-Container

Wichtig für die nächste Session, sonst geht Zeit verloren:

- **Push aus dem Container ist durch GitHub gesperrt (403).** Der Proxy ist gesund, es ist eine
  Berechtigungsgrenze. Der Nutzer pusht **lokal von Windows**.
- **Übergabe von Änderungen per `git format-patch`**, aufgesetzt auf den aktuellen
  Remote-Stand. Vor dem Verschicken in einem Probe-Klon `git am` + `git diff --stat` gegen den
  eigenen Stand prüfen — das hat mehrfach Fehler abgefangen.
- **PowerShell-Eigenheiten:** `git am 0001-*.patch` funktioniert **nicht** (kein Glob für
  externe Programme) — Dateinamen ausschreiben. `del *.patch` dagegen schon.
- **`athleten_daten/` ist im Container leer.** Auswertungen mit echten Athletendaten kann nur
  der Nutzer fahren; im Container geht nur `data/hdts_knie_gesamt.xlsx`.
- **Der Container-Klon kann hinterherhinken.** Vor Aussagen über den Repo-Zustand `git fetch`
  und gegen `origin/...` prüfen, nie den lokalen Klon für maßgeblich halten.

---

## 7. Was zuerst zu tun ist

1. Nachtrag-Commit einspielen und pushen (entfernt die Klarnamen aus Doku und Kommentaren).
2. Entscheiden, ob das Repo privat wird.
3. Erste betreute Live-Session fahren — mit Athletenprofil.
4. Danach §7.2/§7.3 anhand der Eindrücke entscheiden; optional vorher
   `PEAK_PRCT_SAFETY_MIN` bauen, das ist unabhängig.
