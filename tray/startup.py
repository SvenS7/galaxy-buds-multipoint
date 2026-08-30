"""Autorun (Task Scheduler), installatie en uninstall.

Volledig lokaal. Probeert Task Scheduler (betrouwbaarste), fallback naar
Startup-folder lnk/bat en Registry Run.
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
import logging
from pathlib import Path

log = logging.getLogger("startup")

TASK_NAME = "GalaxyBudsMultipointFix"
REG_VALUE = "GalaxyBudsMultipointFix"
STARTUP_BASENAME = "Galaxy Buds Multipoint Fix"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def app_dir() -> Path:
    return Path(__file__).resolve().parent

def appdata_local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))

def logs_dir() -> Path:
    return appdata_local() / "GalaxyBudsMultipoint" / "logs"

def startup_folder() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

def _pythonw_for_task() -> str:
    """Retourneer pythonw.exe pad voor taak. Prefer pythonw naast huidige interpreter."""
    exe = Path(sys.executable)
    # venv: python.exe -> pythonw.exe
    cand = exe.with_name("pythonw.exe")
    if cand.exists():
        return str(cand)
    return str(exe)

def _task_command(minimized: bool = True) -> str:
    """Command string voor schtasks /tr ."""
    pyw = _pythonw_for_task()
    app_py = app_dir() / "app.py"
    # Detecteer uv: als we via uv run draaien, is sys.executable een uv-venv python.
    # Task moet dan pythonw direct aanroepen — uv is niet nodig bij login.
    # We gebruiken daarom direct pythonw + app.py
    args = f'"{pyw}" "{app_py}"'
    if minimized:
        args += " --minimized"
    return args

# ---------------------------------------------------------------------------
# Task Scheduler
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 15) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as exc:
        return 1, "", str(exc)

def task_exists() -> bool:
    rc, out, _ = _run(["schtasks", "/query", "/tn", TASK_NAME], timeout=10)
    return rc == 0

def install_task() -> tuple[bool, str]:
    tr = _task_command(True)
    # /sc onlogon /rl limited /f /it (interactive)
    cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr,
           "/sc", "onlogon", "/rl", "limited", "/f", "/it"]
    rc, out, err = _run(cmd)
    msg = (out + err).strip()
    if rc == 0:
        log.info("Task Scheduler taak aangemaakt: %s", TASK_NAME)
        return True, f"Task Scheduler taak '{TASK_NAME}' aangemaakt"
    log.warning("schtasks create failed rc=%d: %s", rc, msg)
    return False, f"schtasks failed ({rc}): {msg}"

def remove_task() -> tuple[bool, str]:
    if not task_exists():
        return True, f"taak '{TASK_NAME}' bestond niet"
    rc, out, err = _run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    msg = (out + err).strip()
    if rc == 0:
        log.info("taak verwijderd: %s", TASK_NAME)
        return True, f"taak '{TASK_NAME}' verwijderd"
    return False, f"schtasks delete failed ({rc}): {msg}"

# ---------------------------------------------------------------------------
# Startup folder + Registry fallback
# ---------------------------------------------------------------------------

def startup_lnk_path() -> Path:
    return startup_folder() / f"{STARTUP_BASENAME}.lnk"

def startup_bat_path() -> Path:
    return startup_folder() / f"{STARTUP_BASENAME}.bat"

def install_startup_bat() -> tuple[bool, str]:
    """Fallback: .bat in Startup folder (werkt zonder lnk COM)."""
    try:
        startup_folder().mkdir(parents=True, exist_ok=True)
        pyw = _pythonw_for_task()
        app_py = app_dir() / "app.py"
        bat = startup_bat_path()
        bat.write_text(f'@echo off\r\nstart "" "{pyw}" "{app_py}" --minimized\r\n', encoding="utf-8")
        return True, f"Startup .bat aangemaakt: {bat}"
    except Exception as exc:
        return False, str(exc)

def remove_startup_bat() -> tuple[bool, str]:
    removed = []
    for p in [startup_lnk_path(), startup_bat_path()]:
        try:
            if p.exists():
                p.unlink()
                removed.append(str(p))
        except Exception as exc:
            return False, str(exc)
    if removed:
        return True, f"Startup items verwijderd: {', '.join(removed)}"
    return True, "geen Startup items gevonden"

def install_registry() -> tuple[bool, str]:
    try:
        import winreg
        tr = _task_command(True)
        # Registry Run verwacht een command string
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, REG_VALUE, 0, winreg.REG_SZ, tr)
        return True, f"Registry Run key '{REG_VALUE}' gezet"
    except Exception as exc:
        return False, str(exc)

def remove_registry() -> tuple[bool, str]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE | winreg.KEY_READ) as k:
            try:
                winreg.DeleteValue(k, REG_VALUE)
                return True, f"Registry Run key '{REG_VALUE}' verwijderd"
            except FileNotFoundError:
                return True, f"Registry Run key '{REG_VALUE}' bestond niet"
    except Exception as exc:
        return False, str(exc)

def registry_exists() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, REG_VALUE)
            return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Public install/uninstall
# ---------------------------------------------------------------------------

def is_installed() -> bool:
    return task_exists() or startup_lnk_path().exists() or startup_bat_path().exists() or registry_exists()

def install_autorun() -> list[str]:
    """Installeer autorun; retourneer lijst van resultaten (voor dialoog)."""
    results: list[str] = []
    # 1. Task Scheduler (voorkeur)
    ok, msg = install_task()
    results.append(msg)
    if ok:
        # Task gelukt -> geen fallback nodig, maar wel oude fallbacks opruimen not needed
        return results
    # 2. Fallback Startup bat
    ok2, msg2 = install_startup_bat()
    results.append(msg2)
    # 3. Registry als extra fallback
    ok3, msg3 = install_registry()
    results.append(msg3)
    return results

def uninstall_autorun() -> list[str]:
    results: list[str] = []
    for fn in [remove_task, remove_startup_bat, remove_registry]:
        try:
            _, msg = fn()
            results.append(msg)
        except Exception as exc:
            results.append(str(exc))
    return results

def uninstall_all() -> list[str]:
    """Volledige uninstall: autorun + logs + temp.

    Retourneert lijst van wat verwijderd is (voor user dialoog).
    Stoppen van proces doet de caller (tray App).
    """
    msgs: list[str] = []
    msgs.extend(uninstall_autorun())

    # Logs in AppData
    for p in [logs_dir(), appdata_local() / "GalaxyBudsMultipoint"]:
        try:
            if p.exists():
                # behoud folder maar wis inhoud, of wis helemaal?
                shutil.rmtree(p, ignore_errors=False)
                msgs.append(f"logs verwijderd: {p}")
            else:
                msgs.append(f"geen logs op {p}")
        except Exception as exc:
            msgs.append(f"kon {p} niet verwijderen: {exc}")

    # Lokale logs/
    local_logs = app_dir() / "logs"
    try:
        if local_logs.exists():
            for f in local_logs.glob("*.log"):
                try:
                    f.unlink()
                    msgs.append(f"lokaal log verwijderd: {f.name}")
                except Exception as exc:
                    msgs.append(str(exc))
    except Exception as exc:
        msgs.append(str(exc))

    # Temp wake files
    import tempfile, glob
    for pat in [str(Path(tempfile.gettempdir()) / "budsmp-wake-*.wav")]:
        for f in glob.glob(pat):
            try:
                Path(f).unlink(missing_ok=True)
                msgs.append(f"temp verwijderd: {Path(f).name}")
            except Exception:
                pass

    # Eventuele Start Menu shortcut (indien ooit aangemaakt)
    start_menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{STARTUP_BASENAME}.lnk"
    try:
        if start_menu.exists():
            start_menu.unlink()
            msgs.append(f"Start Menu item verwijderd: {start_menu}")
    except Exception as exc:
        msgs.append(str(exc))

    msgs.append("Uninstall voltooid — herstart indien nodig om tray te verbergen")
    return msgs
