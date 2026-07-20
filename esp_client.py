# esp_client.py
"""
AmpelClient: Verbindung zur ESP32-"Ampel" (Timing-Anzeige) - wahlweise ueber
USB-Serial ODER WLAN (TCP). Beide Wege nutzen dasselbe Text-Protokoll.

Spiegelt bewusst das Muster des QiraClient:
  * eigener Hintergrund-Thread zum Lesen,
  * meldet Ereignisse ueber einfache Callbacks (log_fcn etc.) zurueck,
  * die GUI verbindet diese Callbacks mit Qt-Signalen -> thread-sicher.

Abhaengigkeit (nur fuer USB-Serial): pyserial  ->  pip install pyserial
Der WLAN-Weg kommt ohne pyserial aus; das Modul ist auch ohne pyserial
importierbar (wichtig fuer jump_analyzer und die Tests).

Protokoll (siehe Firmware):
  PC -> ESP : PING | SHOW EARLY <1..3> | SHOW LATE <1..3> | SHOW GOOD | OFF |
              POWER ON | POWER OFF | STATUS? | UI ON|OFF | ANALYSIS ON|OFF
  ESP -> PC : READY | PONG | EVENT POWER ON|OFF | OK ... | ERR ...

WLAN-Standard (Access-Point-Modus der Firmware):
  Host = 192.168.4.1   Port = 3333
  (Laptop muss vorher im WLAN "HDTS-Ampel" eingeloggt sein.)

Ampel-Semantik (NEU, phasenabhaengig - passend zum Validierungs-Refactor):
Die Lichter zeigen in BEIDEN Phasen dieselbe ANWEISUNG (wichtig fuer Kinder):
    GELB  (links,  Protokoll "EARLY") = "frueher treten"
    GRUEN (Mitte,  Protokoll "GOOD")  = gut
    BLAU  (rechts, Protokoll "LATE")  = "spaeter treten"
    AUS                                = keine eindeutige Ansage
Achtung: Die fruehere Version dieses Moduls hat den FEHLER angezeigt
(trend>0 -> "zu spaet" -> blau); jetzt wird die Anweisung angezeigt
(trend>0 -> "frueher treten" -> gelb). Wirkt die Richtung auf der Hardware
vertauscht, einfach AMPEL_INVERT_DIRECTION = True setzen.
"""

import math
import time
import socket
import threading

# pyserial ist optional: nur fuer den USB-Transport noetig. Ohne pyserial bleibt
# das Modul importierbar (WLAN + classify_ampel funktionieren trotzdem).
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

# Schwellwerte zentral aus importance_utils ziehen, damit die Ampel EXAKT zur
# phasenabhaengigen Coaching-Ausgabe im JumpAnalyzer passt.
from importance_utils import (
    STEP_THRESHOLD_MEDIUM, STEP_THRESHOLD_STRONG,
    DEADBAND_TREND, CONSISTENCY_GATE,
)

# Falls "frueher"/"spaeter" auf der Hardware vertauscht wirkt, hier True setzen.
AMPEL_INVERT_DIRECTION = False

# WLAN-Standardwerte (Access-Point-Modus der Firmware).
DEFAULT_WIFI_HOST = "192.168.4.1"
DEFAULT_WIFI_PORT = 3333


def _direction_state(trend_score, abs_score):
    """Richtung + Stufe fuer eine eindeutige Timing-Ansage.

    Stufe aus abs_score auf der neuen Sigma-Skala (MAD*1.4826):
        > STEP_THRESHOLD_STRONG (1.9) -> 3, > STEP_THRESHOLD_MEDIUM (1.4) -> 2, sonst 1
    """
    if abs_score > STEP_THRESHOLD_STRONG:
        level = 3
    elif abs_score > STEP_THRESHOLD_MEDIUM:
        level = 2
    else:
        level = 1

    frueher = (trend_score > 0)   # + = "frueher treten" -> gelb/links
    if AMPEL_INVERT_DIRECTION:
        frueher = not frueher
    return ("EARLY", level) if frueher else ("LATE", level)


def classify_ampel(trend_score, abs_score, phase="halten", diffI=None,
                   aufbau_reference_ok=True):
    """Bildet einen Sprung auf einen Ampel-Zustand ab (phasenabhaengig).

    Rueckgabe: (direction, level)
        direction in {"GOOD", "EARLY", "LATE", "OFF"}, level 0..3
        (GELB = EARLY = "frueher treten", BLAU = LATE = "spaeter treten")

    Phase "halten" (Score gegen die Halten-Referenz):
        |trend| < DEADBAND_TREND                     -> GOOD  (Timing stabil)
        trend > +DEADBAND und Konsistenz-Gate ok     -> EARLY (frueher treten)
        trend < -DEADBAND und Konsistenz-Gate ok     -> LATE  (spaeter treten)
        Gate verletzt (|trend|/abs <= CONSISTENCY_GATE) -> OFF

    Phase "aufbau" (Score gegen die AUFBAU-Referenz - Kontakte, die Hoehe
    erzeugt haben; gegen DIESE Referenz ist frueher/spaeter auch im Aufbau
    ein sinnvoller Hinweis):
        diffI > 0                                    -> GOOD  (Erfolg schlaegt
                                                       Muster: Hoehe gewonnen ->
                                                       immer gruen, auch bei
                                                       Timing-Abweichung)
        diffI <= 0 und trend < -DEADBAND             -> LATE  (spaeter/laenger)
        diffI <= 0 und trend > +DEADBAND             -> EARLY (frueher)
        diffI <= 0 und |trend| < DEADBAND            -> OFF

    Fallback ohne individuelle Aufbau-Baseline (aufbau_reference_ok=False):
    Der Goldstandard beschreibt Steady-State-Kontakte und waere als
    Aufbau-Referenz genau falsch. Daher faehrt die Ampel dann NUR das
    diffI-Kriterium (GOOD bei diffI > 0, sonst OFF) - Richtungslichter erst,
    sobald die Aufbau-Baseline steht.

    diffI = NaN (erster Sprung, kein Vorgaenger-Integral) -> OFF, da noch
    keine Aussage ueber den Hoehengewinn moeglich ist.

    Rueckwaertskompatibel: classify_ampel(trend, abs) ohne weitere Argumente
    verhaelt sich wie die Halten-Logik.
    """
    try:
        abs_score = float(abs_score)
        trend_score = float(trend_score)
    except (TypeError, ValueError):
        return ("OFF", 0)
    if not (math.isfinite(abs_score) and math.isfinite(trend_score)):
        return ("OFF", 0)

    if str(phase).lower() == "aufbau":
        try:
            diffI_val = float(diffI)
        except (TypeError, ValueError):
            diffI_val = float("nan")

        if math.isfinite(diffI_val) and diffI_val > 0:
            return ("GOOD", 0)
        if not aufbau_reference_ok:
            return ("OFF", 0)
        if not math.isfinite(diffI_val):
            return ("OFF", 0)
        if abs(trend_score) < DEADBAND_TREND:
            return ("OFF", 0)
        return _direction_state(trend_score, abs_score)

    # Phase "halten"
    if abs(trend_score) < DEADBAND_TREND:
        return ("GOOD", 0)
    consistency = (abs(trend_score) / abs_score) if abs_score > 0 else 0.0
    if consistency <= CONSISTENCY_GATE:
        return ("OFF", 0)
    return _direction_state(trend_score, abs_score)


class _ConnLost(Exception):
    """Interne Ausnahme: Transport (Serial/TCP) ist weggebrochen."""
    pass


class AmpelClient:
    """Ampel-Verbindung mit zwei Transportwegen (serial | wifi)."""

    def __init__(self, log_fcn=print, on_connection_changed=None, on_power_changed=None):
        self._log = log_fcn or (lambda *_: None)
        self._on_conn = on_connection_changed or (lambda *_: None)
        self._on_power = on_power_changed or (lambda *_: None)

        self._mode = None          # None | "serial" | "wifi"
        self._serial = None
        self._sock = None
        self._rx_buffer = b""      # Bytepuffer fuer TCP-Zeilen

        self._reader = None
        self._running = False
        self._connected = False

        self._io_lock = threading.Lock()
        self._pong_event = threading.Event()

    # ---- Portliste (USB) ----
    @staticmethod
    def list_ports():
        if serial is None:
            return []
        ports = []
        for p in serial.tools.list_ports.comports():
            ports.append((p.device, p.description or "Serieller Port"))
        return ports

    # ---- Statusabfrage ----
    def is_connected(self):
        return self._connected and self._mode is not None

    # ---- Verbinden: USB-Serial ----
    def connect(self, port, baud=115200, handshake_timeout=2.5):
        """Oeffnet den seriellen Port, wartet auf Boot und macht einen Handshake."""
        if self.is_connected():
            self._log("Ampel: Bereits verbunden.")
            return True
        if serial is None:
            self._log("Ampel: pyserial fehlt fuer den USB-Modus -> pip install pyserial "
                      "(oder WLAN-Modus verwenden).")
            return False
        if not port:
            self._log("Ampel: Kein COM-Port ausgewaehlt.")
            return False

        try:
            self._serial = serial.Serial(port=port, baudrate=baud,
                                         timeout=0.2, write_timeout=2.0)
        except Exception as e:
            self._log(f"Ampel: Port '{port}' konnte nicht geoeffnet werden: {e}")
            self._serial = None
            return False

        self._mode = "serial"
        self._start_reader()

        # ESP32 resettet beim Oeffnen (DTR/RTS) -> Boot abwarten, dann PINGen.
        time.sleep(0.3)
        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass

        if self._handshake(handshake_timeout):
            self._connected = True
            self._log(f"Ampel: Verbunden (USB) mit {port}.")
            self._safe_call(self._on_conn, True)
            self._raw_send("UI ON")   # ESP -> Status-LED gelb
            return True

        self._log(f"Ampel: Keine Antwort vom ESP an '{port}'. "
                  f"Board/Baudrate (115200)/Firmware pruefen.")
        self._teardown(announce=False)
        return False

    # ---- Verbinden: WLAN (TCP) ----
    def connect_wifi(self, host=DEFAULT_WIFI_HOST, port=DEFAULT_WIFI_PORT, handshake_timeout=4.0):
        """Baut die TCP-Verbindung zum ESP-Access-Point auf."""
        if self.is_connected():
            self._log("Ampel: Bereits verbunden.")
            return True
        if not host:
            self._log("Ampel: Keine ESP-IP angegeben.")
            return False

        try:
            sock = socket.create_connection((host, int(port)), timeout=handshake_timeout)
        except Exception as e:
            self._log(f"Ampel: TCP-Verbindung zu {host}:{port} fehlgeschlagen: {e}")
            self._sock = None
            return False

        sock.settimeout(0.3)         # kurzer Read-Timeout, damit der Thread beendbar bleibt
        self._sock = sock
        self._rx_buffer = b""
        self._mode = "wifi"
        self._start_reader()

        # ESP sendet beim Accept sofort "READY"; zur Sicherheit zusaetzlich PINGen.
        if self._handshake(handshake_timeout):
            self._connected = True
            self._log(f"Ampel: Verbunden (WLAN) mit {host}:{port}.")
            self._safe_call(self._on_conn, True)
            self._raw_send("UI ON")   # ESP -> Status-LED gelb
            return True

        self._log(f"Ampel: Keine Antwort vom ESP unter {host}:{port}. "
                  f"Ist der Laptop im WLAN 'HDTS-Ampel' und die Firmware aktuell?")
        self._teardown(announce=False)
        return False

    # ---- Trennen ----
    def disconnect(self):
        if self._mode is None and not self._connected:
            return
        self._raw_send("UI OFF")  # ESP -> Status-LED zurueck auf rot
        self._raw_send("OFF")     # Anzeige nach Moeglichkeit leeren
        time.sleep(0.05)
        self._teardown(announce=True)
        self._log("Ampel: Verbindung getrennt.")

    # ---- Hochpegelige Sende-Funktionen ----
    def set_analysis(self, on):
        """Meldet dem ESP Start/Stopp der Analyse (Status-LED gruen bzw. gelb)."""
        return self._raw_send("ANALYSIS ON" if on else "ANALYSIS OFF")

    def send_state(self, direction, level=0):
        direction = str(direction).upper()
        if direction in ("OFF", "O"):
            return self._raw_send("OFF")
        if direction == "GOOD":
            return self._raw_send("SHOW GOOD")
        if direction in ("EARLY", "LATE"):
            return self._raw_send(f"SHOW {direction} {int(level)}")
        self._log(f"Ampel: Unbekannte Richtung '{direction}' ignoriert.")
        return False

    def display_off(self):
        return self._raw_send("OFF")

    def set_power(self, on):
        return self._raw_send("POWER ON" if on else "POWER OFF")

    # ---- Intern: Handshake ----
    def _handshake(self, timeout):
        self._pong_event.clear()
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._raw_send("PING")
            if self._pong_event.wait(timeout=0.5):
                return True
        return False

    # ---- Intern: Reader-Thread starten ----
    def _start_reader(self):
        self._running = True
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

    # ---- Intern: Roh-Senden mit Fehlerbehandlung ----
    def _raw_send(self, text):
        payload = (text + "\n").encode("ascii", errors="ignore")
        with self._io_lock:
            try:
                if self._mode == "serial" and self._serial is not None:
                    self._serial.write(payload)
                    return True
                if self._mode == "wifi" and self._sock is not None:
                    self._sock.sendall(payload)
                    return True
                return False
            except Exception as e:
                self._log(f"Ampel: Sendefehler ({e}). Verbindung verloren.")
                threading.Thread(target=lambda: self._teardown(announce=True),
                                 daemon=True).start()
                return False

    # ---- Intern: eine Zeile lesen (transportabhaengig) ----
    def _read_line(self):
        """Gibt eine Zeile (bytes) zurueck oder None bei Timeout. Wirft _ConnLost."""
        if self._mode == "serial":
            try:
                raw = self._serial.readline()
            except Exception as e:
                raise _ConnLost(str(e))
            return raw if raw else None

        # wifi
        while True:
            nl = self._rx_buffer.find(b"\n")
            if nl >= 0:
                line = self._rx_buffer[:nl]
                self._rx_buffer = self._rx_buffer[nl + 1:]
                return line
            try:
                chunk = self._sock.recv(256)
            except socket.timeout:
                return None
            except OSError as e:
                raise _ConnLost(str(e))
            if not chunk:
                raise _ConnLost("Gegenstelle hat geschlossen")
            self._rx_buffer += chunk

    # ---- Intern: Reader-Thread ----
    def _reader_loop(self):
        while self._running:
            try:
                raw = self._read_line()
            except _ConnLost:
                if self._running:
                    self._log("Ampel: Verbindung verloren.")
                    self._teardown(announce=True)
                return
            if raw is None:
                continue
            try:
                line = raw.decode("ascii", errors="replace").strip()
            except Exception:
                continue
            if line:
                self._handle_line(line)

    def _handle_line(self, line):
        upper = line.upper()
        if upper in ("PONG", "READY"):
            self._pong_event.set()
            return
        if upper == "EVENT POWER ON":
            self._log("Ampel: Eingeschaltet (Schalter).")
            self._safe_call(self._on_power, True)
            return
        if upper == "EVENT POWER OFF":
            self._log("Ampel: Ausgeschaltet (Schalter).")
            self._safe_call(self._on_power, False)
            return
        if upper.startswith("ERR"):
            self._log(f"Ampel-Fehler: {line}")
            return
        # OK-/STATUS-Zeilen still ignorieren, um den Log nicht zu fluten.

    # ---- Intern: sauberer Abbau ----
    def _teardown(self, announce):
        was_connected = self._connected
        self._connected = False
        self._running = False

        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        self._reader = None

        with self._io_lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
            self._rx_buffer = b""
            self._mode = None

        if announce and was_connected:
            self._safe_call(self._on_conn, False)

    # ---- Intern: Callback robust aufrufen ----
    def _safe_call(self, fcn, *args):
        try:
            fcn(*args)
        except Exception as e:
            self._log(f"Ampel: Callback-Fehler: {e}")
