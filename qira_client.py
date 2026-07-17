import threading
import queue
import numpy as np
import websocket


class QiraClient:
    """
    QiraClient: Verbindet sich mit der Qira-Software ueber Websockets, empfaengt die Live-Daten
    von der Kraftmessplatte, verarbeitet sie und stellt sie fuer die Analyse im JumpAnalyzer bereit.

    NEU: Die Trampolin-Auswahl erfolgt jetzt MANUELL ueber die GUI.
    Die fruehere automatische Auswahl anhand der Kraftsummen wurde entfernt, da sie
    in der Praxis nicht zuverlaessig funktioniert hat.

      * Trampolin 1 -> Sensoren 1-4  (Spalten 0:4)
      * Trampolin 2 -> Sensoren 5-8  (Spalten 4:8)

    Die Auswahl wird ueber set_trampoline("T1" | "T2") gesetzt.
    Ohne gesetzte Auswahl werden KEINE Daten in die Queue gelegt (und eine Warnung geloggt).
    """

    # ---- 1. Initialisierung ----
    def __init__(self, url, logFcn=print):
        self.url = url
        self.logFcn = logFcn
        self.data_queue = queue.Queue()

        # blockID zeigt an, ob neue Daten da sind.
        self.blockID = 0

        # Manuell gesetzte Trampolin-Auswahl: "T1", "T2" oder None (keine Auswahl).
        self.selected_trampoline = None

        # Damit die Warnung "keine Auswahl" nicht den Log flutet.
        self._warned_no_selection = False

        # WebsocketApp mit den passenden Callback-Funktionen einrichten
        self.ws = websocket.WebSocketApp(
            self.url,
            on_open=self.on_open,
            on_message=self.on_text_message,
            on_error=self.on_error,
            on_close=self.on_close
        )

        self.thread = None

    # ---- 1b. Manuelle Trampolin-Auswahl setzen ----
    def set_trampoline(self, trampoline):
        """Setzt die aktive Platte manuell. Erlaubt: 'T1', 'T2' oder None.

        Wird von der GUI aufgerufen, wenn der Nutzer einen der beiden
        Trampolin-Buttons drueckt.
        """
        if trampoline not in ("T1", "T2", None):
            self.logFcn(f"QiraClient: Ungueltige Trampolin-Auswahl '{trampoline}' ignoriert.")
            return
        self.selected_trampoline = trampoline
        self._warned_no_selection = False
        if trampoline is not None:
            self.logFcn(f"QiraClient: Trampolin {trampoline[-1]} manuell ausgewaehlt.")

    # ---- 2. Verbindung wird eingerichtet ----
    def connect(self):
        """Startet den Websocket-Client in einem eigenen Hintergrund-Thread."""
        self.thread = threading.Thread(target=self.ws.run_forever)
        self.thread.daemon = True
        self.thread.start()
        self.logFcn("Verbindungsversuch gestartet...")

    # ---- 3. Callback: Verbindung geoeffnet ----
    def on_open(self, ws):
        self.logFcn("Qira ist verbunden. Bereit fuer Analyse...")

    # ---- 4. Callback: Textnachricht empfangen ----
    def on_text_message(self, ws, message):
        try:
            # Von Qira empfangene Daten in ein 2D-Array umwandeln
            values = np.array([
                list(map(float, row.split()))
                for row in message.strip().splitlines()
                if row.strip()
            ])

            # Format-Check - erwartet wird ein 48x8 Array
            if values.ndim != 2 or values.shape[1] != 8:
                self.logFcn("Unerwartetes Datenformat von der Kraftmessplatte")
                return

            # Ohne manuelle Auswahl werden keine Daten weitergegeben.
            if self.selected_trampoline not in ("T1", "T2"):
                if not self._warned_no_selection:
                    self.logFcn("QiraClient: Keine Trampolin-Auswahl gesetzt - Daten werden verworfen. "
                                "Bitte Trampolin 1 oder 2 waehlen.")
                    self._warned_no_selection = True
                return

            # Summen je Platte (Achse 1 = Zeilensumme fuer jeden der 48 Frames)
            if self.selected_trampoline == "T1":
                output = np.sum(values[:, 0:4], axis=1)   # Sensoren 1-4
            else:  # "T2"
                output = np.sum(values[:, 4:8], axis=1)   # Sensoren 5-8

            # In die Queue legen, damit der JumpAnalyzer (im GUI-Timer) sie abholen kann.
            self.data_queue.put((self.blockID, output))
            self.blockID += 1

        except Exception as e:
            self.logFcn(f"Fehler bei der Live-Datenverarbeitung: {e}")

    # ---- 5. Callback: Fehler ----
    def on_error(self, ws, error):
        self.logFcn(f"Websocket Fehler: {error}")

    # ---- 6. Callback: Verbindung geschlossen ----
    def on_close(self, ws, close_status_code, close_msg):
        self.logFcn(f"Verbindung zu Qira geschlossen: {close_msg}")
