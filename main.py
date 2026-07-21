import os
import sys
import html
import subprocess
import pandas as pd
import numpy as np
import qtawesome as qta
from datetime import datetime
from PyQt6.QtCore import QTimer, QObject, pyqtSignal, QSize, Qt
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QTextEdit, QVBoxLayout, QWidget,
    QFrame, QComboBox, QInputDialog, QMessageBox, QHBoxLayout, QLabel, QFileDialog,
    QLineEdit,
)
from PyQt6.QtGui import QIcon

# Klassen-Imports
from jump_analyzer import JumpAnalyzer
from qira_client import QiraClient
from esp_client import AmpelClient, DEFAULT_WIFI_HOST
from profiler import run_offline_profiler, ALL_COLUMNS, BASELINE_COLUMNS
from baseline_manager import update_athlete_baseline
import session_storage
import qira_launcher

# Qira-Autostart: Wartezeit (Sekunden), bis Qira im Vordergrund ist und die
# Leertaste (Mess-Modus) sinnvoll ankommt. Am echten Laptop ggf. anpassen.
QIRA_START_KEY_DELAY_S = 6.0

# Stil fuer die Trampolin-Buttons (dunkelgrau = inaktiv, hellgrau = aktiv).
TRAMPOLIN_STYLE_INACTIVE = (
    "background-color: #3A3A3F; color: #C8C8CC; font-weight: bold; "
    "border-radius: 6px; padding: 10px;"
)
TRAMPOLIN_STYLE_ACTIVE = (
    "background-color: #B8B8BD; color: #121214; font-weight: bold; "
    "border: 2px solid #00B0FF; border-radius: 6px; padding: 10px;"
)


# Stil fuer den Verbindungs-Status ("Getrennt" rot, "Verbunden" gruen).
# Wird sowohl fuer die Ampel- als auch die Qira-Statuszeile genutzt.
AMPEL_STATUS_DISCONNECTED = "color: #FF3B30; font-weight: bold;"
AMPEL_STATUS_CONNECTED = "color: #00E676; font-weight: bold;"

# Protokoll: Farbe je Wichtigkeit (info neutral-grau, success gruen, warning gelb, error rot).
LOG_COLORS = {
    "info": "#B8B8BD",
    "success": "#00E676",
    "warning": "#FFC107",
    "error": "#FF5252",
}

# Dashboard: Zuordnung Ampel-Richtung -> (Anzeigetext, Kachel-Hintergrund, Textfarbe).
# Spiegelt die ESP32-Ampel: GELB=frueher, BLAU=spaeter, GRUEN=gut, AUS=keine Ansage.
AMPEL_TILE_MAP = {
    "GOOD":  ("GUT",           "#00E676", "#0A0A0A"),
    "EARLY": ("FRÜHER TRETEN", "#FFAA00", "#0A0A0A"),
    "LATE":  ("SPÄTER TRETEN", "#6A00E0", "#FFFFFF"),
    "OFF":   ("—",             "#2A2A2E", "#8A8A90"),
}
# Neutraler Leerzustand der Kacheln (vor dem ersten Sprung / nach Analyse-Stopp).
DASHBOARD_EMPTY = ("—", "#2A2A2E", "#8A8A90")


# Ein Signal-Verteiler, um Thread-Sicherheit fuer die GUI zu garantieren
class SignalBridge(QObject):
    log_signal = pyqtSignal(str)
    # Tatsaechlicher Qira-Verbindungsstatus (True=verbunden, False=getrennt/fehlgeschlagen).
    # Kommt aus dem Websocket-Thread und muss in den GUI-Thread gehoben werden.
    qira_connection_signal = pyqtSignal(bool)
    # Ampel-Ereignisse kommen aus dem Reader-Thread des AmpelClient und muessen
    # ueber Signale in den GUI-Thread gehoben werden.
    ampel_conn_signal = pyqtSignal(bool)
    ampel_power_signal = pyqtSignal(bool)
    # Per-Sprung-Infos aus dem Analyzer (laufen im Timer/GUI-Thread, werden aber
    # ueber das Signal einheitlich thread-sicher an das Dashboard geliefert).
    jump_signal = pyqtSignal(dict)
    # Protokollzeile aus Hintergrund-Threads (z.B. Qira-Autostart), Reihenfolge
    # (level, text) - thread-sicher in den GUI-Thread gehoben.
    log_level_signal = pyqtSignal(str, str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("HDTS Analyse - Live")
        # Fenster nutzt die volle nutzbare Bildschirmhoehe; Breite so, dass die
        # zwei Verbindungs-Karten bequem nebeneinander passen. Bleibt skalierbar.
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else None
        win_w = 980
        win_h = avail.height() if avail is not None else 900
        self.resize(win_w, win_h)
        self.setMinimumWidth(880)
        if avail is not None:
            self.move(avail.x() + max(0, (avail.width() - win_w) // 2), avail.y())
        self.setWindowIcon(QIcon("app_icon.ico"))

        # ---- 1. Signal-Bridge ----
        self.bridge = SignalBridge()
        self.bridge.log_signal.connect(self.log_message)
        self.bridge.qira_connection_signal.connect(self.on_qira_connection_changed)
        self.bridge.jump_signal.connect(self.on_jump_update)
        self.bridge.log_level_signal.connect(self._on_bg_log)

        # ---- 2. Qira-Client und JumpAnalyzer ----
        self._qira_connected = False
        self.client = QiraClient(
            url="ws://localhost:8081", logFcn=self.bridge.log_signal.emit,
            on_connection_changed=self.bridge.qira_connection_signal.emit)
        self.analyzer = JumpAnalyzer()

        # ---- 2b. Ampel-Client (ESP32) ----
        self.bridge.ampel_conn_signal.connect(self.on_ampel_connection_changed)
        self.bridge.ampel_power_signal.connect(self.on_ampel_power_changed)
        self.ampel = AmpelClient(
            log_fcn=self.bridge.log_signal.emit,
            on_connection_changed=self.bridge.ampel_conn_signal.emit,
            on_power_changed=self.bridge.ampel_power_signal.emit,
        )
        # Analyzer sendet nach jedem Sprung den Ampel-Zustand; ohne Verbindung
        # sind die Sendeaufrufe im AmpelClient wirkungslos (kein Fehler).
        self.analyzer.set_ampel_client(self.ampel)
        # Analyzer liefert nach jedem Sprung Schnellinfos ans Dashboard (thread-
        # sicher ueber die SignalBridge in den GUI-Thread gehoben).
        self.analyzer.set_on_jump(self.bridge.jump_signal.emit)
        # Transportweg der Ampel ("USB" / "WLAN")
        self.ampel_mode = "USB"
        # Bearbeitungs-Modus fuer Port/IP (Default aus: nur Umschalten + Verbinden).
        self.ampel_edit_mode = False

        self.gui_last_block_id = -1
        self.midterm_storage = []

        # Manuell gewaehltes Trampolin ("T1" / "T2" / None)
        self.selected_trampoline = None

        # ---- 3. GUI Layout ----
        # Einheitliche Karten-Optik: jeder Abschnitt sitzt in einer "card"-QFrame
        # mit gleichem Rahmen/Radius/Innenabstand und optionalem Titel.
        root = QVBoxLayout()
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(16)

        def make_card(title=None):
            card = QFrame()
            card.setObjectName("card")
            inner = QVBoxLayout(card)
            inner.setContentsMargins(20, 16, 20, 18)
            inner.setSpacing(12)
            if title:
                lbl = QLabel(title)
                lbl.setObjectName("sectionTitle")
                inner.addWidget(lbl)
            return card, inner

        # ===============================================================
        # Zwei Spalten (enden unten buendig, danach breit Dashboard + Log):
        #   LINKS  (oben->unten): Verbindung Qira, Athlet, Trampolin
        #   RECHTS (oben->unten): Verbindung Ampel, Analyse-Button, Session-Button
        # ===============================================================

        # --- LINKS 1: Verbindung Qira (kompakt) ---
        qira_card, qira_layout = make_card("Verbindung Qira")
        self.btn_connect = QPushButton("Mit Qira verbinden")
        self.btn_connect.setObjectName("primaryButton")
        self.btn_connect.setIcon(qta.icon("msc.link", color="white"))
        self.btn_connect.setIconSize(QSize(20, 20))
        self.btn_connect.clicked.connect(self.start_connection)
        qira_layout.addWidget(self.btn_connect)
        self.lbl_qira_status = QLabel("Getrennt")
        self.lbl_qira_status.setStyleSheet(AMPEL_STATUS_DISCONNECTED)
        qira_layout.addWidget(self.lbl_qira_status)

        # --- LINKS 2: Athlet ---
        athlet_card, athlet_layout = make_card("Athlet auswählen")
        dropdown_row = QHBoxLayout()
        dropdown_row.setSpacing(12)
        athlet_icon = QLabel()
        athlet_icon.setPixmap(qta.icon("msc.person", color="#00B0FF").pixmap(QSize(28, 28)))
        athlet_icon.setObjectName("athletIcon")
        athlet_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dropdown_athleten = QComboBox()
        self.dropdown_athleten.setObjectName("athletDropdown")
        dropdown_row.addWidget(athlet_icon, stretch=1)
        dropdown_row.addWidget(self.dropdown_athleten, stretch=9)
        athlet_layout.addLayout(dropdown_row)

        # --- LINKS 3: Trampolin ---
        tramp_card, tramp_layout = make_card("Trampolin")
        tramp_row = QHBoxLayout()
        tramp_row.setSpacing(12)
        self.btn_tramp1 = QPushButton("Trampolin 1")
        self.btn_tramp2 = QPushButton("Trampolin 2")
        self.btn_tramp1.setStyleSheet(TRAMPOLIN_STYLE_INACTIVE)
        self.btn_tramp2.setStyleSheet(TRAMPOLIN_STYLE_INACTIVE)
        self.btn_tramp1.clicked.connect(lambda: self.select_trampoline("T1"))
        self.btn_tramp2.clicked.connect(lambda: self.select_trampoline("T2"))
        tramp_row.addWidget(self.btn_tramp1)
        tramp_row.addWidget(self.btn_tramp2)
        tramp_layout.addLayout(tramp_row)

        # --- RECHTS 1: Verbindung Ampel ---
        ampel_card, ampel_layout = make_card("Verbindung Ampel")

        # Transport-Umschalter USB / WLAN + kleiner Bearbeiten-Button.
        # Normalfall: Port wird automatisch gewaehlt, IP ist voreingestellt - die
        # editierbaren Zeilen erscheinen erst im Bearbeitungs-Modus.
        ampel_mode_row = QHBoxLayout()
        ampel_mode_row.setSpacing(12)
        self.btn_ampel_usb = QPushButton("USB")
        self.btn_ampel_wifi = QPushButton("WLAN")
        self.btn_ampel_usb.setStyleSheet(TRAMPOLIN_STYLE_ACTIVE)
        self.btn_ampel_wifi.setStyleSheet(TRAMPOLIN_STYLE_INACTIVE)
        self.btn_ampel_usb.clicked.connect(lambda: self.select_ampel_mode("USB"))
        self.btn_ampel_wifi.clicked.connect(lambda: self.select_ampel_mode("WLAN"))
        self.btn_ampel_edit = QPushButton()
        self.btn_ampel_edit.setObjectName("iconButton")
        self.btn_ampel_edit.setIcon(qta.icon("msc.edit", color="#C8C8CC"))
        self.btn_ampel_edit.setIconSize(QSize(18, 18))
        self.btn_ampel_edit.setFixedWidth(44)
        self.btn_ampel_edit.setToolTip("Port/IP bearbeiten")
        self.btn_ampel_edit.clicked.connect(self.toggle_ampel_edit)
        ampel_mode_row.addWidget(self.btn_ampel_usb, stretch=1)
        ampel_mode_row.addWidget(self.btn_ampel_wifi, stretch=1)
        ampel_mode_row.addWidget(self.btn_ampel_edit)
        ampel_layout.addLayout(ampel_mode_row)

        # Kompakte Infozeile (Normalmodus): zeigt, womit verbunden wird.
        self.lbl_ampel_target = QLabel("")
        self.lbl_ampel_target.setObjectName("mutedInfo")
        ampel_layout.addWidget(self.lbl_ampel_target)

        # USB-Zeile: COM-Port-Auswahl + Aktualisieren (nur im Bearbeitungs-Modus).
        self.ampel_usb_row = QWidget()
        usb_row_layout = QHBoxLayout(self.ampel_usb_row)
        usb_row_layout.setContentsMargins(0, 0, 0, 0)
        usb_row_layout.setSpacing(12)
        self.dropdown_ampel_port = QComboBox()
        self.dropdown_ampel_port.setObjectName("athletDropdown")
        self.dropdown_ampel_port.currentIndexChanged.connect(
            lambda _idx: self._update_ampel_target_label())
        self.btn_ampel_refresh = QPushButton("Aktualisieren")
        self.btn_ampel_refresh.clicked.connect(self.refresh_ampel_ports)
        usb_row_layout.addWidget(self.dropdown_ampel_port, stretch=7)
        usb_row_layout.addWidget(self.btn_ampel_refresh, stretch=3)
        ampel_layout.addWidget(self.ampel_usb_row)
        self.ampel_usb_row.setVisible(False)

        # WLAN-Zeile: IP-Feld (Standard: Access-Point-IP der Firmware; nur im Edit-Modus).
        self.ampel_wifi_row = QWidget()
        wifi_row_layout = QHBoxLayout(self.ampel_wifi_row)
        wifi_row_layout.setContentsMargins(0, 0, 0, 0)
        wifi_row_layout.setSpacing(12)
        wifi_row_layout.addWidget(QLabel("ESP-IP:"))
        self.input_ampel_ip = QLineEdit(DEFAULT_WIFI_HOST)
        self.input_ampel_ip.textChanged.connect(
            lambda _t: self._update_ampel_target_label())
        wifi_row_layout.addWidget(self.input_ampel_ip, stretch=1)
        ampel_layout.addWidget(self.ampel_wifi_row)
        self.ampel_wifi_row.setVisible(False)

        # Verbinden-Button + Statuszeile
        self.btn_ampel_connect = QPushButton("Mit Ampel verbinden")
        self.btn_ampel_connect.setObjectName("primaryButton")
        self.btn_ampel_connect.clicked.connect(self.toggle_ampel_connection)
        ampel_layout.addWidget(self.btn_ampel_connect)

        self.lbl_ampel_status = QLabel("Getrennt")
        self.lbl_ampel_status.setStyleSheet(AMPEL_STATUS_DISCONNECTED)
        ampel_layout.addWidget(self.lbl_ampel_status)
        # Ports einlesen + automatisch ersten waehlen, Infozeile/Sichtbarkeit setzen.
        self.refresh_ampel_ports()
        self._update_ampel_editor_visibility()

        # --- RECHTS 2+3: Aktionsbuttons ---
        self.btn_analyze = QPushButton("Analyse starten")
        self.btn_analyze.setObjectName("primaryButton")
        self.btn_analyze.setIcon(qta.icon("msc.play", color="white"))
        self.btn_analyze.setIconSize(QSize(20, 20))
        self.btn_analyze.clicked.connect(self.start_analysis)
        # Erst klickbar, wenn Qira + Ampel verbunden UND ein Trampolin gewaehlt ist.
        self.btn_analyze.setEnabled(False)

        self.btn_viewer = QPushButton("Gespeicherte Session ansehen")
        self.btn_viewer.setObjectName("primaryButton")
        self.btn_viewer.setIcon(qta.icon("msc.graph-line", color="white"))
        self.btn_viewer.setIconSize(QSize(20, 20))
        self.btn_viewer.clicked.connect(self.open_session_viewer)

        # --- Spalten zusammensetzen ---
        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(16)
        left_col.addWidget(qira_card)
        left_col.addWidget(athlet_card)
        left_col.addWidget(tramp_card)
        left_col.addStretch(1)

        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(16)
        right_col.addWidget(ampel_card)
        # Stretch schiebt die Aktionsbuttons an den unteren Rand, damit die rechte
        # Spalte auf gleicher Hoehe endet wie die (hoehere) linke Spalte.
        right_col.addStretch(1)
        right_col.addWidget(self.btn_analyze)
        right_col.addWidget(self.btn_viewer)

        left_wrap = QWidget()
        left_wrap.setLayout(left_col)
        right_wrap = QWidget()
        right_wrap.setLayout(right_col)

        columns_row = QHBoxLayout()
        columns_row.setSpacing(16)
        columns_row.addWidget(left_wrap, stretch=1)
        columns_row.addWidget(right_wrap, stretch=1)
        root.addLayout(columns_row)

        # ---------------------------------------------------------------
        # Zeile 4: Dashboard - Schnellinfos zum aktuellen Sprung (KPI-Kacheln)
        # ---------------------------------------------------------------
        dash_card, dash_layout = make_card("Aktueller Sprung")

        def make_tile(caption, object_name="kpiTile"):
            tile = QFrame()
            tile.setObjectName(object_name)
            tv = QVBoxLayout(tile)
            tv.setContentsMargins(16, 14, 16, 14)
            tv.setSpacing(6)
            cap = QLabel(caption)
            cap.setObjectName("kpiCaption")
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val = QLabel("—")
            val.setObjectName("kpiValue")
            val.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val.setWordWrap(True)
            tv.addWidget(cap)
            tv.addWidget(val)
            return tile, val

        self.tile_jump, self.lbl_jump_value = make_tile("Sprung")
        self.tile_phase, self.lbl_phase_value = make_tile("Phase")
        self.tile_ampel, self.lbl_ampel_value = make_tile("Ampel-Anweisung", "kpiTileAmpel")
        # Phase-Wert im Cyan-Akzent, damit die Karte einheitlich zum Theme passt.
        self.lbl_phase_value.setStyleSheet("color: #00B0FF;")

        tiles_row = QHBoxLayout()
        tiles_row.setSpacing(16)
        tiles_row.addWidget(self.tile_jump, stretch=1)
        tiles_row.addWidget(self.tile_phase, stretch=1)
        tiles_row.addWidget(self.tile_ampel, stretch=2)
        dash_layout.addLayout(tiles_row)
        root.addWidget(dash_card)
        self._reset_dashboard()

        # ---------------------------------------------------------------
        # Zeile 5: Protokoll (fuellt die restliche Hoehe)
        # ---------------------------------------------------------------
        log_card, log_layout = make_card("Protokoll")
        self.log_viewer = QTextEdit()
        self.log_viewer.setReadOnly(True)
        log_layout.addWidget(self.log_viewer, stretch=1)
        root.addWidget(log_card, stretch=1)

        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

        # ---- 4. Timer ----
        self.timer = QTimer()
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.check_for_live_data)

        # ---- 5. Athletenliste ----
        self.load_athleten_list()
        self.dropdown_athleten.currentIndexChanged.connect(self.on_athlet_changed)

        self.log_message("System bereit. Athlet und Trampolin wählen.", level="info")

        # ---- 6. Qira automatisch mitstarten (UWP-App) + Mess-Modus (Leertaste) ----
        # Laeuft im Hintergrund; danach versucht die App, sich selbststaendig mit
        # Qira zu verbinden (mit Wiederholungen, da Qira ein paar Sekunden braucht).
        self._qira_autoconnect_attempts = 0
        self._qira_autoconnect_max = 12
        self.qira_connect_timer = QTimer()
        self.qira_connect_timer.setInterval(3000)
        self.qira_connect_timer.timeout.connect(self._qira_autoconnect_tick)
        self._start_qira_autostart()

    # ---- 5b. Qira-Autostart + automatischer Verbindungsaufbau ----
    def _start_qira_autostart(self):
        """Startet Qira (Best effort) und beginnt danach den Auto-Verbindungsversuch."""
        qira_launcher.autostart_qira(
            log=self.bridge.log_level_signal.emit,
            key_delay_s=QIRA_START_KEY_DELAY_S,
        )
        # Erst nach kurzer Wartezeit mit dem Verbinden beginnen (Qira bootet noch).
        QTimer.singleShot(2000, self.qira_connect_timer.start)

    def _qira_autoconnect_tick(self):
        """Versucht periodisch, sich mit Qira zu verbinden, bis es klappt."""
        if self._qira_connected:
            self.qira_connect_timer.stop()
            return
        if self._qira_autoconnect_attempts >= self._qira_autoconnect_max:
            self.qira_connect_timer.stop()
            self.log_message("Qira nicht automatisch erreichbar – bitte manuell verbinden.",
                             level="warning")
            return
        self._qira_autoconnect_attempts += 1
        # start_connection() baut (nur wenn getrennt) einen neuen Verbindungsversuch auf.
        self.start_connection()

    # ---- 6. Log ----
    def log_message(self, text, level=None):
        """Schreibt eine Protokollzeile, farblich nach Wichtigkeit.

        level: "info" | "success" | "warning" | "error". Wird kein Level
        uebergeben (z.B. bei Meldungen aus Sub-Modulen, die nur einen String
        liefern), wird es heuristisch am Text erkannt.
        """
        if level is None:
            level = self._detect_log_level(text)
        color = LOG_COLORS.get(level, LOG_COLORS["info"])
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe = html.escape(str(text))
        self.log_viewer.append(
            f'<span style="color:{color};">{timestamp} &nbsp;·&nbsp; {safe}</span>')

    def _on_bg_log(self, level, text):
        """Adapter: Hintergrund-Threads liefern (level, text); log_message will (text, level)."""
        self.log_message(text, level)

    @staticmethod
    def _detect_log_level(text):
        """Heuristische Level-Erkennung fuer Meldungen ohne explizites Level."""
        t = str(text).lower()
        if "fehler" in t or "fehlgeschlagen" in t or "konnte nicht" in t:
            return "error"
        if ("warnung" in t or "kein referenzsatz" in t or "nicht gestartet" in t
                or "verworfen" in t or "nichts gespeichert" in t):
            return "warning"
        if ("verbunden" in t or "erfolgreich" in t or "gespeichert" in t
                or "aktualisiert" in t or "bereit" in t):
            return "success"
        return "info"

    # ---- 6b. Trampolin-Auswahl ----
    def select_trampoline(self, trampoline):
        """Setzt die manuelle Trampolin-Auswahl, markiert den Button hellgrau und
        gibt die Auswahl an den QiraClient weiter. Immer nur ein Button aktiv."""
        self.selected_trampoline = trampoline
        self.btn_tramp1.setStyleSheet(
            TRAMPOLIN_STYLE_ACTIVE if trampoline == "T1" else TRAMPOLIN_STYLE_INACTIVE)
        self.btn_tramp2.setStyleSheet(
            TRAMPOLIN_STYLE_ACTIVE if trampoline == "T2" else TRAMPOLIN_STYLE_INACTIVE)
        if self.client is not None:
            self.client.set_trampoline(trampoline)
        self._update_analyze_enabled()

    # ---- 6b2. "Analyse starten" nur bei erfuellten Vorbedingungen freigeben ----
    def _update_analyze_enabled(self):
        """Aktiviert den Analyse-Button nur, wenn Qira UND Ampel verbunden sind
        und ein Trampolin gewaehlt ist. Waehrend einer laufenden Analyse bleibt
        der Button aktiv (damit man immer stoppen kann)."""
        if self.btn_analyze.text() == "Analyse stoppen":
            self.btn_analyze.setEnabled(True)
            return
        ready = (self._qira_connected
                 and self.ampel.is_connected()
                 and self.selected_trampoline in ("T1", "T2"))
        self.btn_analyze.setEnabled(ready)

    # ---- 6c. Ampel: Transport-Umschalter ----
    def select_ampel_mode(self, mode):
        """Schaltet zwischen USB und WLAN um. Nur im getrennten Zustand moeglich."""
        if self.ampel.is_connected():
            self.log_message("Ampel: Umschalten nur im getrennten Zustand möglich.", level="warning")
            return
        self.ampel_mode = mode
        self.btn_ampel_usb.setStyleSheet(
            TRAMPOLIN_STYLE_ACTIVE if mode == "USB" else TRAMPOLIN_STYLE_INACTIVE)
        self.btn_ampel_wifi.setStyleSheet(
            TRAMPOLIN_STYLE_ACTIVE if mode == "WLAN" else TRAMPOLIN_STYLE_INACTIVE)
        # Bei USB die Ports frisch einlesen und automatisch den ersten waehlen.
        if mode == "USB":
            self.refresh_ampel_ports()
        self._update_ampel_editor_visibility()
        self._update_ampel_target_label()

    # ---- 6c2. Ampel: Bearbeitungs-Modus fuer Port/IP umschalten ----
    def toggle_ampel_edit(self):
        """Blendet die editierbare Port-/IP-Zeile ein bzw. aus. Bei bestehender
        Verbindung gesperrt (dann sind Port/IP ohnehin fixiert)."""
        if self.ampel.is_connected():
            self.log_message("Ampel: Bearbeiten nur im getrennten Zustand möglich.", level="warning")
            return
        self.ampel_edit_mode = not self.ampel_edit_mode
        self._update_ampel_editor_visibility()

    # ---- 6c3. Ampel: Sichtbarkeit Editor-Zeilen vs. Infozeile ----
    def _update_ampel_editor_visibility(self):
        editing = self.ampel_edit_mode and not self.ampel.is_connected()
        self.ampel_usb_row.setVisible(editing and self.ampel_mode == "USB")
        self.ampel_wifi_row.setVisible(editing and self.ampel_mode == "WLAN")
        # Infozeile nur im Normalmodus (sonst zeigt die Editor-Zeile die Auswahl).
        self.lbl_ampel_target.setVisible(not editing)
        # Bearbeiten-Button optisch aktiv, solange der Modus laeuft.
        self.btn_ampel_edit.setStyleSheet(
            TRAMPOLIN_STYLE_ACTIVE if editing else "")

    # ---- 6c4. Ampel: Infozeile "womit verbunden wird" aktualisieren ----
    def _update_ampel_target_label(self):
        if self.ampel_mode == "WLAN":
            ip = self.input_ampel_ip.text().strip() or DEFAULT_WIFI_HOST
            self.lbl_ampel_target.setText(f"WLAN  ·  IP: {ip}")
        else:
            port = self.dropdown_ampel_port.currentData()
            if port:
                self.lbl_ampel_target.setText(f"USB  ·  Port: {port} (automatisch)")
            else:
                self.lbl_ampel_target.setText("USB  ·  kein Port gefunden - 'Bearbeiten' → 'Aktualisieren'")

    # ---- 6d. Ampel: COM-Ports aktualisieren (+ ersten automatisch waehlen) ----
    def refresh_ampel_ports(self):
        self.dropdown_ampel_port.blockSignals(True)
        self.dropdown_ampel_port.clear()
        ports = AmpelClient.list_ports()
        if not ports:
            self.dropdown_ampel_port.addItem("Kein Port gefunden", None)
        else:
            for device, description in ports:
                self.dropdown_ampel_port.addItem(f"{device}  -  {description}", device)
            self.dropdown_ampel_port.setCurrentIndex(0)   # automatisch ersten Port
        self.dropdown_ampel_port.blockSignals(False)
        self._update_ampel_target_label()

    # ---- 6e. Ampel: Verbinden / Trennen ----
    def toggle_ampel_connection(self):
        if self.ampel.is_connected():
            self.ampel.disconnect()
            return

        if self.ampel_mode == "WLAN":
            host = self.input_ampel_ip.text().strip() or DEFAULT_WIFI_HOST
            self.ampel.connect_wifi(host=host)
        else:
            port = self.dropdown_ampel_port.currentData()
            if port is None:
                self.log_message("Ampel: Kein USB-Port gefunden. Im Bearbeiten-Modus "
                                 "aktualisieren oder auf WLAN umschalten.", level="warning")
                return
            self.ampel.connect(port)

    # ---- 6f. Ampel: Callbacks (via SignalBridge im GUI-Thread) ----
    def on_ampel_connection_changed(self, connected):
        if connected:
            transport = "WLAN" if self.ampel_mode == "WLAN" else "USB"
            self.lbl_ampel_status.setText(f"Verbunden ({transport}) - Ampel AN")
            self.lbl_ampel_status.setStyleSheet(AMPEL_STATUS_CONNECTED)
            self.btn_ampel_connect.setText("Ampel trennen")
            # Wie beim Qira-Button: verbunden -> rot ("trennen").
            self.btn_ampel_connect.setStyleSheet(
                "background-color: #FF3B30; color: white; font-weight: bold; "
                "border-radius: 6px; padding: 10px;")
        else:
            self.lbl_ampel_status.setText("Getrennt")
            self.lbl_ampel_status.setStyleSheet(AMPEL_STATUS_DISCONNECTED)
            self.btn_ampel_connect.setText("Mit Ampel verbinden")
            self.btn_ampel_connect.setStyleSheet("")   # zurueck auf primaryButton-Blau
        # Waehrend einer Verbindung sind Moduswechsel und Bearbeiten gesperrt.
        self.btn_ampel_usb.setEnabled(not connected)
        self.btn_ampel_wifi.setEnabled(not connected)
        self.btn_ampel_edit.setEnabled(not connected)
        if connected and self.ampel_edit_mode:
            self.ampel_edit_mode = False
        self._update_ampel_editor_visibility()
        # Ampel-Verbindung ist eine der Vorbedingungen fuer die Analyse.
        self._update_analyze_enabled()

    def on_ampel_power_changed(self, powered_on):
        if self.ampel.is_connected():
            transport = "WLAN" if self.ampel_mode == "WLAN" else "USB"
            state = "AN" if powered_on else "AUS (Schalter)"
            self.lbl_ampel_status.setText(f"Verbunden ({transport}) - Ampel {state}")

    # ---- 7. Verbindungslogik ----
    def start_connection(self):
        """Startet bzw. trennt die Qira-Verbindung.

        Der Button wird NICHT mehr optimistisch umgeschaltet, sondern erst,
        wenn der Websocket den echten Verbindungsstatus meldet
        (on_qira_connection_changed via SignalBridge).
        """
        if not self._qira_connected:
            try:
                self.client = QiraClient(
                    url="ws://localhost:8081", logFcn=self.bridge.log_signal.emit,
                    on_connection_changed=self.bridge.qira_connection_signal.emit)
                # Bereits getroffene Trampolin-Auswahl auf den neuen Client uebertragen.
                if self.selected_trampoline is not None:
                    self.client.set_trampoline(self.selected_trampoline)
            except Exception as e:
                self.log_message(f"Qira: Verbindung konnte nicht vorbereitet werden ({e}).",
                                 level="error")
                return

            self.client.connect()
        else:
            if hasattr(self.client, 'ws') and self.client.ws:
                try:
                    self.client.ws.close()
                except Exception as e:
                    self.log_message(f"Qira: Trennen fehlgeschlagen ({e}).", level="error")

    # ---- 7b. Qira: Verbindungsstatus-Callback (via SignalBridge im GUI-Thread) ----
    def on_qira_connection_changed(self, connected):
        """Schaltet den Verbinden-Button anhand des ECHTEN Verbindungsstatus um."""
        self._qira_connected = connected
        if connected:
            self.btn_connect.setStyleSheet(
                "background-color: #FF3B30; color: white; font-weight: bold; "
                "border-radius: 6px; padding: 10px;")
            self.btn_connect.setText("Verbindung trennen")
            disconnect_icon = qta.icon(
                "msc.link", "msc.remove-close",
                options=[{"color": "white", "scale_factor": 1.0},
                {"color": "white", "scale_factor": 1.5}])
            self.btn_connect.setIcon(disconnect_icon)
            self.lbl_qira_status.setText("Verbunden")
            self.lbl_qira_status.setStyleSheet(AMPEL_STATUS_CONNECTED)
            # Verbindung steht -> automatische Wiederholversuche beenden.
            if hasattr(self, "qira_connect_timer"):
                self.qira_connect_timer.stop()
        else:
            self.btn_connect.setStyleSheet("")
            self.btn_connect.setText("Mit Qira verbinden")
            self.btn_connect.setIcon(qta.icon("msc.link", color="white"))
            self.btn_connect.setIconSize(QSize(20, 20))
            self.lbl_qira_status.setText("Getrennt")
            self.lbl_qira_status.setStyleSheet(AMPEL_STATUS_DISCONNECTED)
        # Qira-Verbindung ist eine der Vorbedingungen fuer die Analyse.
        self._update_analyze_enabled()

    # ---- 7c. Dashboard: Kacheln aktualisieren / zuruecksetzen ----
    def _apply_ampel_tile(self, direction):
        """Setzt Text, Hintergrund und Textfarbe der Ampel-Kachel je Zustand."""
        text, bg, fg = AMPEL_TILE_MAP.get(str(direction).upper(), DASHBOARD_EMPTY)
        self.lbl_ampel_value.setText(text)
        self.lbl_ampel_value.setStyleSheet(f"color: {fg}; background-color: transparent;")
        self.tile_ampel.setStyleSheet(
            f"#kpiTileAmpel {{ background-color: {bg}; border-radius: 10px; }}")

    def _reset_dashboard(self):
        """Leerzustand vor dem ersten Sprung / nach Analyse-Stopp."""
        self.lbl_jump_value.setText("—")
        self.lbl_phase_value.setText("—")
        self._apply_ampel_tile("OFF")

    def on_jump_update(self, payload):
        """Aktualisiert die Dashboard-Kacheln mit den Infos zum aktuellen Sprung
        (thread-sicher via SignalBridge aus dem Analyzer-Callback)."""
        jump_no = payload.get("jump_no", 0)
        phase = str(payload.get("phase", "")).lower()
        direction = payload.get("ampel_direction", "OFF")

        self.lbl_jump_value.setText(f"#{jump_no}")
        if phase == "aufbau":
            self.lbl_phase_value.setText("AUFBAU")
        elif phase == "halten":
            self.lbl_phase_value.setText("HALTEN")
        else:
            self.lbl_phase_value.setText("—")
        self._apply_ampel_tile(direction)

    # ---- 8. Analyse-Start/Stop ----
    def start_analysis(self):
        selected_data = self.dropdown_athleten.currentData()
        athlet_name = "master_session_daten" if selected_data == "global" else self.dropdown_athleten.currentText()

        if self.btn_analyze.text() == "Analyse starten":
            # Pflichtpruefung: Trampolin muss gewaehlt sein.
            if self.selected_trampoline not in ("T1", "T2"):
                QMessageBox.warning(self, "Keine Trampolin-Auswahl",
                                    "Bitte zuerst Trampolin 1 oder Trampolin 2 auswaehlen, "
                                    "bevor die Analyse gestartet wird.")
                self.log_message("Analyse nicht gestartet: kein Trampolin gewählt.",
                                 level="warning")
                return

            self.btn_analyze.setStyleSheet(
                "background-color: #FF3B30; color: white; font-weight: bold; "
                "border-radius: 6px; padding: 10px;")
            self.btn_analyze.setText("Analyse stoppen")
            self.btn_analyze.setIcon(qta.icon("msc.debug-pause", color="white"))
            self.btn_analyze.setIconSize(QSize(20, 20))

            # Saubere Ausgangslage
            self.gui_last_block_id = self.client.blockID
            self.analyzer.reset()
            self._reset_dashboard()
            self.midterm_storage = []
            self.client.blockID = 0
            # WICHTIG: manuelle Auswahl NICHT zuruecksetzen, nur erneut anwenden.
            self.client.set_trampoline(self.selected_trampoline)

            while not self.client.data_queue.empty():
                self.client.data_queue.get()

            self.analyzer.load_profile(athlet_name, logFcn=self.bridge.log_signal.emit)

            self.timer.start()
            # Ampel: Status-LED auf "Analyse laeuft" (gruen); ohne Verbindung wirkungslos.
            self.ampel.set_analysis(True)
            self.log_message(f"Analyse gestartet – Trampolin {self.selected_trampoline[-1]}. "
                             f"Warte auf Daten von Qira.", level="success")
        else:
            self.btn_analyze.setStyleSheet("")
            self.btn_analyze.setText("Analyse starten")
            self.btn_analyze.setIcon(qta.icon("msc.play", color="white"))
            self.btn_analyze.setIconSize(QSize(20, 20))

            self.timer.stop()
            # Ampel: Status-LED zurueck auf "UI verbunden" (gelb), Anzeige leeren.
            self.ampel.set_analysis(False)
            self.ampel.display_off()
            self.log_message("Analyse gestoppt.", level="info")
            # Nach dem Stoppen wieder anhand der Vorbedingungen freigeben/sperren.
            self._update_analyze_enabled()

            if hasattr(self, 'midterm_storage') and len(self.midterm_storage) > 0:
                # Gemeinsamer Zeitstempel fuer CSV und Session-Datei.
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                self.log_message("Werte Aufzeichnung aus …", level="info")
                status_meldung = run_offline_profiler(
                    raw_signal=self.midterm_storage,
                    athlet_name=athlet_name,
                    fs=self.analyzer.fs_file,
                    peak_height=self.analyzer.peak_height,
                    peak_distance=self.analyzer.peak_distance,
                    a=self.analyzer.a, b=self.analyzer.b,
                    session_id=timestamp, timestamp=timestamp,
                )
                self.log_message(status_meldung)

                # ---- NEU: Volle Analysekurve als Session speichern ----
                _, session_meldung = session_storage.save_session(
                    raw_signal=self.midterm_storage,
                    athlet_name=athlet_name,
                    fs=self.analyzer.fs_file,
                    peak_height=self.analyzer.peak_height,
                    peak_distance=self.analyzer.peak_distance,
                    a=self.analyzer.a, b=self.analyzer.b,
                    selected_trampoline=self.selected_trampoline,
                    timestamp=timestamp,
                )
                self.log_message(session_meldung)

                self.midterm_storage = []

                if selected_data != "global":
                    self.log_message("Aktualisiere Athleten-Referenz …", level="info")
                    self.log_message(update_athlete_baseline(athlet_name))
            else:
                self.log_message("Keine Sprungdaten aufgezeichnet.", level="warning")

    # ---- 8b. Session-Viewer oeffnen ----
    def open_session_viewer(self):
        """Oeffnet einen Datei-Dialog und startet session_viewer.py als Subprozess."""
        start_dir = os.path.join("athleten_daten", "sessions")
        if not os.path.isdir(start_dir):
            start_dir = os.getcwd()
        path, _ = QFileDialog.getOpenFileName(
            self, "Gespeicherte Session waehlen", start_dir, "Session-Dateien (*.npz)")
        if not path:
            return
        viewer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_viewer.py")
        try:
            subprocess.Popen([sys.executable, viewer, path])
            self.log_message(f"Session-Viewer geöffnet: {os.path.basename(path)}", level="info")
        except Exception as e:
            self.log_message(f"Session-Viewer konnte nicht geöffnet werden ({e}).", level="error")

    # ---- 8c. Sauberes Beenden ----
    def closeEvent(self, event):
        """Trennt die Ampel-Verbindung sauber, bevor das Fenster schliesst."""
        try:
            if self.ampel.is_connected():
                self.ampel.disconnect()
        except Exception:
            pass
        super().closeEvent(event)

    # ---- 9. Live-Datenabfrage ----
    def check_for_live_data(self):
        while not self.client.data_queue.empty():
            blockID, live_data = self.client.data_queue.get()
            if live_data is not None:
                self.handle_qira_data(live_data, blockID)

    # ---- 10. Datenverarbeitung ----
    def handle_qira_data(self, data_block, blockID):
        self.analyzer.process(data_block, blockID, logFcn=self.bridge.log_signal.emit)
        if self.btn_analyze.text() == "Analyse stoppen":
            # Rohes (summiertes) Signal fuer Profiler/Session sammeln.
            self.midterm_storage.extend(np.asarray(data_block).flatten())

    # ---- 11. Athletenliste laden ----
    def load_athleten_list(self):
        self.dropdown_athleten.blockSignals(True)
        self.dropdown_athleten.clear()
        self.dropdown_athleten.addItem("Profi-Standard (Master)", "global")

        folder = "athleten_daten"
        os.makedirs(folder, exist_ok=True)

        for file in os.listdir(folder):
            if file.lower().endswith(".csv"):
                name = os.path.splitext(file)[0]
                if name == "master_session_daten":
                    continue
                if name.endswith("_baseline") or name.endswith("_all"):
                    continue
                self.dropdown_athleten.addItem(name, file)

        self.dropdown_athleten.addItem("+ Neuen Athleten hinzufuegen...", "neu")
        self.dropdown_athleten.blockSignals(False)

    # ---- 12. Athletenauswahl / neues Profil ----
    def on_athlet_changed(self, index):
        data = self.dropdown_athleten.itemData(index)

        if data == "neu":
            name, ok = QInputDialog.getText(self, "Neuer Athlet", "Name des Athleten eingeben:")
            if ok and name.strip():
                clean_name = name.strip().replace(" ", "_").lower()
                filepath = os.path.join("athleten_daten", f"{clean_name}.csv")
                all_path = os.path.join("athleten_daten", f"{clean_name}_all.csv")

                if os.path.exists(filepath):
                    QMessageBox.warning(self, "Fehler", "Dieser Athlet existiert bereits!")
                    self.dropdown_athleten.setCurrentIndex(0)
                else:
                    os.makedirs("athleten_daten", exist_ok=True)
                    # Baseline-relevante CSV (hochwertige Spruenge)
                    pd.DataFrame(columns=BASELINE_COLUMNS).to_csv(filepath, index=False)
                    # Vollstaendige _all.csv (alle Spruenge) gleich mit korrekter Struktur anlegen
                    if not os.path.exists(all_path):
                        pd.DataFrame(columns=ALL_COLUMNS).to_csv(all_path, index=False)

                    self.log_message(f"Athlet '{name}' angelegt.", level="success")
                    self.load_athleten_list()
                    idx = self.dropdown_athleten.findText(clean_name)
                    if idx >= 0:
                        self.dropdown_athleten.setCurrentIndex(idx)
            else:
                self.dropdown_athleten.setCurrentIndex(0)
        else:
            self.log_message(f"Athlet gewählt: {self.dropdown_athleten.currentText()}", level="info")


if __name__ == "__main__":
    app = QApplication(sys.argv)

    modern_style = """
        QMainWindow { background-color: #121214; }
        QWidget {
            background-color: #121214; color: #E1E1E6;
            font-family: 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 10pt;
        }
        /* Labels erben sonst den Fenster-Hintergrund und wuerden auf farbigen
           Kacheln als dunkler Balken erscheinen. */
        QLabel { background: transparent; }
        QPushButton#primaryButton {
            background-color: #00B0FF; color: #ffffff; font-weight: bold;
            border-radius: 6px; padding: 10px 20px; font-size: 11pt; letter-spacing: 0.5px;
        }
        QPushButton#primaryButton:hover { background-color: #33BEFF; }
        QPushButton#primaryButton:disabled {
            background-color: #23252B; color: #5A5A62;
        }
        QPushButton#iconButton {
            background-color: #3A3A3F; border: none; border-radius: 6px; padding: 8px;
        }
        QPushButton#iconButton:hover { background-color: #4A4A50; }
        QLabel#mutedInfo { color: #8A8A90; font-size: 9pt; }
        QTextEdit {
            background-color: #1A1A1E; border: 1px solid #29292E; border-radius: 8px;
            padding: 10px; color: #00E676;
            font-family: 'Consolas', 'Courier New', monospace; font-size: 9pt;
        }
        /* Einheitliche Karten-Optik fuer alle Abschnitte. */
        QFrame#card, QFrame#athletPanel {
            background-color: #16171B; border: 1px solid #30333A; border-radius: 14px;
        }
        QLabel#sectionTitle { color: #FFFFFF; font-size: 14pt; font-weight: bold; }
        QLabel#athletIcon { color: #00B0FF; font-size: 22px; min-width: 32px; }
        QComboBox#athletDropdown {
            background-color: #0E1014; color: #FFFFFF; border: 1px solid #343842;
            border-radius: 10px; padding: 10px 14px; font-size: 11pt;
        }
        QComboBox#athletDropdown::drop-down { border: none; width: 32px; }
        /* Dashboard-Kacheln (KPI). */
        QFrame#kpiTile, QFrame#kpiTileAmpel {
            background-color: #0E1014; border: 1px solid #2A2D34; border-radius: 10px;
        }
        QLabel#kpiCaption {
            color: #8A8A90; font-size: 9pt; font-weight: bold; letter-spacing: 1px;
        }
        QLabel#kpiValue { color: #FFFFFF; font-size: 20pt; font-weight: bold; }
    """
    app.setStyleSheet(modern_style)

    window = MainWindow()
    window.log_viewer.setFrameShape(QFrame.Shape.NoFrame)
    window.show()
    sys.exit(app.exec())
