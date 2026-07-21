# qira_launcher.py
"""
Startet die Qira-Software beim App-Start automatisch mit.

Besonderheit: Qira ist eine Windows-Store-/UWP-App und liegt unter
C:\\Program Files\\WindowsApps (gesperrter Ordner) - es gibt keinen nutzbaren
Datei-Pfad. UWP-Apps startet man ueber ihre AUMID (AppUserModelID) via
"shell:AppsFolder\\<AUMID>". Die AUMID wird zur Laufzeit per PowerShell
(Get-StartApps) ueber den Anzeigenamen ermittelt - so ist kein Ordnerzugriff
und kein fest verdrahteter Pfad noetig.

Nach dem Start wird optional der Mess-Modus ausgeloest, was manuell einem
Leertasten-Druck entspricht (SendKeys ' ' an das dann fokussierte Qira-Fenster).

WICHTIG: Dieser Windows-/UWP-Teil laesst sich nur am echten Windows-Rechner
verifizieren. Alle Schritte sind defensiv (nichts wirft nach aussen), laufen im
Hintergrund-Thread (blockieren die GUI nie) und melden jeden Schritt ueber den
log-Callback. Timing der Leertaste ggf. ueber key_delay_s justieren.
"""

import sys
import time
import threading
import subprocess

# Anzeigename-Muster fuer Get-StartApps (-like). Bei Bedarf anpassen.
QIRA_APP_NAME_PATTERN = "*Qira*"


def _run_powershell(command, timeout=20):
    """Fuehrt ein PowerShell-Kommando aus und gibt (stdout, ok) zurueck."""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=timeout,
    )
    return completed.stdout.strip(), (completed.returncode == 0)


def find_qira_aumid(name_pattern=QIRA_APP_NAME_PATTERN):
    """Ermittelt die AUMID der Qira-App ueber ihren Startmenue-Namen (oder None)."""
    command = (
        "$a = Get-StartApps | "
        f"Where-Object {{ $_.Name -like '{name_pattern}' }} | "
        "Select-Object -First 1 -ExpandProperty AppID; "
        "if ($a) { Write-Output $a }"
    )
    out, _ok = _run_powershell(command)
    return out or None


def launch_qira(aumid):
    """Startet die UWP-App ueber ihre AUMID (shell:AppsFolder)."""
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{aumid}"])


def send_start_key():
    """Sendet einen Leertasten-Druck an das aktuell fokussierte Fenster."""
    _run_powershell("(New-Object -ComObject WScript.Shell).SendKeys(' ')")


def autostart_qira(log, name_pattern=QIRA_APP_NAME_PATTERN, key_delay_s=6.0, send_key=True):
    """Startet Qira (Best effort) im Hintergrund-Thread und loest optional den
    Mess-Modus per Leertaste aus.

    log: Callback log(level, text) mit level in {"info","warning","error"}.
    Rueckgabe: der gestartete Thread (v.a. fuer Tests).
    """
    def worker():
        if sys.platform != "win32":
            log("info", "Qira-Autostart ist nur unter Windows verfügbar.")
            return
        try:
            aumid = find_qira_aumid(name_pattern)
        except Exception as e:
            log("warning", f"Qira: Autostart nicht möglich ({e}). Bitte manuell starten.")
            return
        if not aumid:
            log("warning", "Qira: Anwendung nicht im Startmenü gefunden. Bitte manuell starten.")
            return
        try:
            launch_qira(aumid)
            log("info", "Qira wird gestartet …")
        except Exception as e:
            log("warning", f"Qira: Start fehlgeschlagen ({e}). Bitte manuell starten.")
            return
        if send_key:
            time.sleep(max(0.0, float(key_delay_s)))
            try:
                send_start_key()
                log("info", "Qira: Mess-Modus ausgelöst (Leertaste).")
            except Exception as e:
                log("warning", f"Qira: Leertaste konnte nicht gesendet werden ({e}).")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread
