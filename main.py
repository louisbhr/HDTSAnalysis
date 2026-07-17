import os
import sys
import subprocess
import pandas as pd
import numpy as np
import qtawesome as qta
from datetime import datetime
from PyQt6.QtCore import QTimer, QObject, pyqtSignal, QSize, Qt
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QTextEdit, QVBoxLayout, QWidget,
    QFrame, QComboBox, QInputDialog, QMessageBox, QHBoxLayout, QLabel, QFileDialog,
)
from PyQt6.QtGui import QIcon

# Klassen-Imports
from jump_analyzer import JumpAnalyzer
from qira_client import QiraClient
from profiler import run_offline_profiler, ALL_COLUMNS, BASELINE_COLUMNS
from baseline_manager import update_athlete_baseline
import session_storage

# Stil fuer die Trampolin-Buttons (dunkelgrau = inaktiv, hellgrau = aktiv).
TRAMPOLIN_STYLE_INACTIVE = (
    "background-color: #3A3A3F; color: #C8C8CC; font-weight: bold; "
    "border-radius: 6px; padding: 10px;"
)
TRAMPOLIN_STYLE_ACTIVE = (
    "background-color: #B8B8BD; color: #121214; font-weight: bold; "
    "border: 2px solid #00B0FF; border-radius: 6px; padding: 10px;"
)


# Ein Signal-Verteiler, um Thread-Sicherheit fuer die GUI zu garantieren
class SignalBridge(QObject):
    log_signal = pyqtSignal(str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("HDTS Analyse - Live")
        self.resize(600, 640)
        self.setWindowIcon(QIcon("app_icon.ico"))

        # ---- 1. Signal-Bridge ----
        self.bridge = SignalBridge()
        self.bridge.log_signal.connect(self.log_message)

        # ---- 2. Qira-Client und JumpAnalyzer ----
        self.client = QiraClient(url="ws://localhost:8081", logFcn=self.bridge.log_signal.emit)
        self.analyzer = JumpAnalyzer()

        self.gui_last_block_id = -1
        self.midterm_storage = []

        # Manuell gewaehltes Trampolin ("T1" / "T2" / None)
        self.selected_trampoline = None

        # ---- 3. GUI Layout ----
        layout = QVBoxLayout()

        self.btn_connect = QPushButton("Mit Qira verbinden")
        self.btn_connect.setObjectName("primaryButton")
        self.btn_connect.setIcon(qta.icon("msc.link", color="white"))
        self.btn_connect.setIconSize(QSize(20, 20))
        self.btn_connect.clicked.connect(self.start_connection)
        layout.addWidget(self.btn_connect)

        # Athletenauswahl Panel
        athlet_panel = QFrame()
        athlet_panel.setObjectName("athletPanel")
        athlet_layout = QVBoxLayout(athlet_panel)
        athlet_layout.setContentsMargins(20, 16, 20, 20)
        athlet_layout.setSpacing(12)

        lbl_athlet = QLabel("Athlet auswählen")
        lbl_athlet.setObjectName("sectionTitle")
        athlet_layout.addWidget(lbl_athlet)

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
        layout.addWidget(athlet_panel)

        # ---- NEU: Trampolin-Auswahl (zwischen Athletenauswahl und "Analyse starten") ----
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
        layout.addLayout(tramp_row)

        # Analyse starten Button
        self.btn_analyze = QPushButton("Analyse starten")
        self.btn_analyze.setObjectName("primaryButton")
        self.btn_analyze.setIcon(qta.icon("msc.play", color="white"))
        self.btn_analyze.setIconSize(QSize(20, 20))
        self.btn_analyze.clicked.connect(self.start_analysis)
        layout.addWidget(self.btn_analyze)

        # ---- NEU: Button, um gespeicherte Sessions anzusehen ----
        self.btn_viewer = QPushButton("Gespeicherte Session ansehen")
        self.btn_viewer.setObjectName("primaryButton")
        self.btn_viewer.setIcon(qta.icon("msc.graph-line", color="white"))
        self.btn_viewer.setIconSize(QSize(20, 20))
        self.btn_viewer.clicked.connect(self.open_session_viewer)
        layout.addWidget(self.btn_viewer)

        # Log-Viewer
        self.log_viewer = QTextEdit()
        self.log_viewer.setReadOnly(True)
        layout.addWidget(self.log_viewer)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # ---- 4. Timer ----
        self.timer = QTimer()
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.check_for_live_data)

        # ---- 5. Athletenliste ----
        self.load_athleten_list()
        self.dropdown_athleten.currentIndexChanged.connect(self.on_athlet_changed)

        self.log_message("System bereit. Bitte mit Qira verbinden und ein Trampolin waehlen...")

    # ---- 6. Log ----
    def log_message(self, text):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_viewer.append(f"{timestamp} >> {text}")

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

    # ---- 7. Verbindungslogik ----
    def start_connection(self):
        if "trennen" not in self.btn_connect.text().lower():
            self.btn_connect.setStyleSheet(
                "background-color: #FF3B30; color: white; font-weight: bold; "
                "border-radius: 6px; padding: 10px;")
            self.btn_connect.setText("Verbindung trennen")
            disconnect_icon = qta.icon(
                "msc.link", "msc.remove-close",
                options=[{"color": "white", "scale_factor": 1.0},
                {"color": "white", "scale_factor": 1.5}])
            self.btn_connect.setIcon(disconnect_icon)

            try:
                self.client = QiraClient(url="ws://localhost:8081", logFcn=self.bridge.log_signal.emit)
                # Bereits getroffene Trampolin-Auswahl auf den neuen Client uebertragen.
                if self.selected_trampoline is not None:
                    self.client.set_trampoline(self.selected_trampoline)
            except Exception as e:
                self.log_message(f"Fehler beim Re-Initialisieren des Clients: {e}")

            self.client.connect()
        else:
            self.btn_connect.setStyleSheet("")
            self.btn_connect.setText("Mit Qira verbinden")
            self.btn_connect.setIcon(qta.icon("msc.link", color="white"))
            self.btn_connect.setIconSize(QSize(20, 20))

            if hasattr(self.client, 'ws') and self.client.ws:
                try:
                    self.client.ws.close()
                except Exception as e:
                    self.log_message(f"Fehler beim Schliessen des Sockets: {e}")

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
                self.log_message("WARNUNG: Analyse nicht gestartet - kein Trampolin ausgewaehlt.")
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
            self.midterm_storage = []
            self.client.blockID = 0
            # WICHTIG: manuelle Auswahl NICHT zuruecksetzen, nur erneut anwenden.
            self.client.set_trampoline(self.selected_trampoline)

            while not self.client.data_queue.empty():
                self.client.data_queue.get()

            self.analyzer.load_profile(athlet_name, logFcn=self.bridge.log_signal.emit)

            self.timer.start()
            self.log_message(f"Analyse gestartet (Trampolin {self.selected_trampoline[-1]}). "
            f"Warte auf Daten von Qira...")
        else:
            self.btn_analyze.setStyleSheet("")
            self.btn_analyze.setText("Analyse starten")
            self.btn_analyze.setIcon(qta.icon("msc.play", color="white"))
            self.btn_analyze.setIconSize(QSize(20, 20))

            self.timer.stop()
            self.log_message("Analyse gestoppt.")

            if hasattr(self, 'midterm_storage') and len(self.midterm_storage) > 0:
                # Gemeinsamer Zeitstempel fuer CSV und Session-Datei.
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                self.log_message("Verarbeite Gesamtaufzeichnung im Hintergrund-Profiler...")
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
                    self.log_message("Aktualisiere Athleten-Baseline und Feature Importance...")
                    self.log_message(update_athlete_baseline(athlet_name))
            else:
                self.log_message("Keine Daten im midterm_storage gefunden.")

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
            self.log_message(f"Session-Viewer geoeffnet fuer: {os.path.basename(path)}")
        except Exception as e:
            self.log_message(f"Fehler beim Oeffnen des Viewers: {e}")

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

                    self.log_message(f"Profil fuer '{name}' wurde erfolgreich angelegt.")
                    self.load_athleten_list()
                    idx = self.dropdown_athleten.findText(clean_name)
                    if idx >= 0:
                        self.dropdown_athleten.setCurrentIndex(idx)
            else:
                self.dropdown_athleten.setCurrentIndex(0)
        else:
            self.log_message(f"Profil gewechselt zu: {self.dropdown_athleten.currentText()}")


if __name__ == "__main__":
    app = QApplication(sys.argv)

    modern_style = """
        QMainWindow { background-color: #121214; }
        QWidget {
            background-color: #121214; color: #E1E1E6;
            font-family: 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 10pt;
        }
        QPushButton#primaryButton {
            background-color: #00B0FF; color: #ffffff; font-weight: bold;
            border-radius: 6px; padding: 10px 20px; font-size: 11pt; letter-spacing: 0.5px;
        }
        QTextEdit {
            background-color: #1A1A1E; border: 1px solid #29292E; border-radius: 8px;
            padding: 10px; color: #00E676;
            font-family: 'Consolas', 'Courier New', monospace; font-size: 9pt;
        }
        QFrame#athletPanel {
            background-color: transparent; border: 1px solid #30333A; border-radius: 14px;
        }
        QLabel#sectionTitle { color: #FFFFFF; font-size: 14pt; font-weight: bold; }
        QLabel#athletIcon { color: #00B0FF; font-size: 22px; min-width: 32px; }
        QComboBox#athletDropdown {
            background-color: #0E1014; color: #FFFFFF; border: 1px solid #343842;
            border-radius: 10px; padding: 10px 14px; font-size: 11pt;
        }
        QComboBox#athletDropdown::drop-down { border: none; width: 32px; }
    """
    app.setStyleSheet(modern_style)

    window = MainWindow()
    window.log_viewer.setFrameShape(QFrame.Shape.NoFrame)
    window.show()
    sys.exit(app.exec())
