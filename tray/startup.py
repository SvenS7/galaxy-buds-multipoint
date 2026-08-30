"""Autorun (Task Scheduler), install and uninstall.

Fully local. Tries Task Scheduler (most reliable), fallback to
Startup folder lnk/bat and Registry Run.
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
TASK_NAME_BOOT = "GalaxyBudsMultipointFixBoot"
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
    """Return pythonw.exe path for task. Prefer pythonw next to current interpreter."""
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    if cand.exists():
        return str(cand)
    return str(exe)

def _task_command(minimized: bool = True) -> str:
    """Command string for schtasks /tr ."""
    pyw = _pythonw_for_task()
    app_py = app_dir() / "app.py"
    args = f'"{pyw}" "{app_py}"'
    if minimized:
        args += " --minimized"
    return args

def _task_parts() -> tuple[str, str]:
    """Split command into (pythonw.exe, arguments) for XML."""
    pyw = _pythonw_for_task()
    app_py = str(app_dir() / "app.py")
    args = f'"{app_py}" --minimized'
    return pyw, args

def _task_xml() -> str:
    """Task XML with both LogonTrigger and BootTrigger (reliable at boot and login)."""
    pyw, args = _task_parts()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\\{TASK_NAME}</URI>
    <Description>Galaxy Buds multipoint fix — applies asVer=2 on connect</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{pyw}</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>"""

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
    # Check both logon and boot task (either counts as installed)
    for name in (TASK_NAME, TASK_NAME_BOOT):
        rc, _, _ = _run(["schtasks", "/query", "/tn", name], timeout=10)
        if rc == 0:
            return True
    return False

def _task_exists_name(name: str) -> bool:
    rc, _, _ = _run(["schtasks", "/query", "/tn", name], timeout=10)
    return rc == 0

def install_task() -> tuple[bool, str]:
    # 1. Try XML with both LogonTrigger and BootTrigger (real boot+login)
    try:
        import tempfile
        xml = _task_xml()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-16") as f:
            f.write(xml)
            xml_path = f.name
        try:
            cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/xml", xml_path, "/f"]
            rc, out, err = _run(cmd, timeout=20)
            msg = (out + err).strip()
            if rc == 0:
                log.info("Task Scheduler task (XML, logon+boot) created: %s", TASK_NAME)
                return True, f"Task Scheduler task '{TASK_NAME}' created (logon + boot)"
            log.warning("XML task create failed rc=%d: %s — fallback to /sc", rc, msg)
        finally:
            try:
                Path(xml_path).unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as exc:
        log.warning("XML task path failed: %s — fallback to /sc", exc)

    # 2. Fallback: classic /sc onlogon + separate boot task
    tr = _task_command(True)
    results: list[str] = []
    ok_any = False

    cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr,
           "/sc", "onlogon", "/rl", "limited", "/f", "/it"]
    rc, out, err = _run(cmd)
    msg = (out + err).strip()
    if rc == 0:
        log.info("Task Scheduler task created (onlogon): %s", TASK_NAME)
        results.append(f"Task Scheduler task '{TASK_NAME}' (onlogon) created")
        ok_any = True
    else:
        log.warning("schtasks onlogon failed rc=%d: %s", rc, msg)
        results.append(f"schtasks onlogon failed ({rc}): {msg}")

    cmd2 = ["schtasks", "/create", "/tn", TASK_NAME_BOOT, "/tr", tr,
            "/sc", "onstart", "/rl", "limited", "/f"]
    rc2, out2, err2 = _run(cmd2)
    msg2 = (out2 + err2).strip()
    if rc2 == 0:
        log.info("Task Scheduler boot task created: %s", TASK_NAME_BOOT)
        results.append(f"Boot task '{TASK_NAME_BOOT}' (onstart) created")
        ok_any = True
    else:
        log.info("schtasks onstart result rc=%d: %s", rc2, msg2)

    if ok_any:
        return True, " | ".join(results)
    return False, " | ".join(results)

def remove_task() -> tuple[bool, str]:
    msgs: list[str] = []
    found = False
    for name in (TASK_NAME, TASK_NAME_BOOT):
        if not _task_exists_name(name):
            msgs.append(f"task '{name}' did not exist")
            continue
        found = True
        rc, out, err = _run(["schtasks", "/delete", "/tn", name, "/f"])
        msg = (out + err).strip()
        if rc == 0:
            log.info("task removed: %s", name)
            msgs.append(f"task '{name}' removed")
        else:
            msgs.append(f"schtasks delete {name} failed ({rc}): {msg}")
    if not found:
        return True, "no Task Scheduler tasks found"
    return True, " | ".join(msgs)

# ---------------------------------------------------------------------------
# Startup folder + Registry fallback
# ---------------------------------------------------------------------------

def startup_lnk_path() -> Path:
    return startup_folder() / f"{STARTUP_BASENAME}.lnk"

def startup_bat_path() -> Path:
    return startup_folder() / f"{STARTUP_BASENAME}.bat"

def install_startup_bat() -> tuple[bool, str]:
    """Fallback: .bat in Startup folder (works without lnk COM)."""
    try:
        startup_folder().mkdir(parents=True, exist_ok=True)
        pyw = _pythonw_for_task()
        app_py = app_dir() / "app.py"
        bat = startup_bat_path()
        bat.write_text(f'@echo off\r\nstart "" "{pyw}" "{app_py}" --minimized\r\n', encoding="utf-8")
        return True, f"Startup .bat created: {bat}"
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
        return True, f"Startup items removed: {', '.join(removed)}"
    return True, "no Startup items found"

def install_registry() -> tuple[bool, str]:
    try:
        import winreg
        tr = _task_command(True)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, REG_VALUE, 0, winreg.REG_SZ, tr)
        return True, f"Registry Run key '{REG_VALUE}' set"
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
                return True, f"Registry Run key '{REG_VALUE}' removed"
            except FileNotFoundError:
                return True, f"Registry Run key '{REG_VALUE}' did not exist"
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
    """Install autorun; return list of results (for dialog)."""
    results: list[str] = []
    ok, msg = install_task()
    results.append(msg)
    if ok:
        return results
    ok2, msg2 = install_startup_bat()
    results.append(msg2)
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
    """Full uninstall: all autorun entries (logon+boot) + all logs + temp."""
    msgs: list[str] = []
    msgs.extend(uninstall_autorun())

    for name in (TASK_NAME, TASK_NAME_BOOT):
        try:
            rc, _, _ = _run(["schtasks", "/query", "/tn", name], timeout=8)
            if rc == 0:
                rc2, out2, err2 = _run(["schtasks", "/delete", "/tn", name, "/f"], timeout=10)
                if rc2 == 0:
                    msgs.append(f"task '{name}' additionally removed")
        except Exception:
            pass

    for p in [startup_lnk_path(), startup_bat_path(),
              startup_folder() / f"{TASK_NAME}.lnk",
              startup_folder() / f"{TASK_NAME}.bat"]:
        try:
            if p.exists():
                p.unlink()
                msgs.append(f"Startup item removed: {p.name}")
        except Exception as exc:
            msgs.append(str(exc))

    for p in [logs_dir(), appdata_local() / "GalaxyBudsMultipoint"]:
        try:
            if p.exists():
                shutil.rmtree(p, ignore_errors=False)
                msgs.append(f"logs removed: {p}")
            else:
                msgs.append(f"no logs at {p}")
        except Exception as exc:
            msgs.append(f"could not remove {p}: {exc}")

    for cfg in [appdata_local() / "GalaxyBudsMultipoint" / "config.json",
                Path(os.environ.get("APPDATA", "")) / "GalaxyBudsMultipoint" / "config.json"]:
        try:
            if cfg.exists():
                cfg.unlink()
                msgs.append(f"config removed: {cfg}")
        except Exception as exc:
            msgs.append(str(exc))

    local_logs = app_dir() / "logs"
    try:
        if local_logs.exists():
            for f in local_logs.glob("*.log"):
                try:
                    f.unlink()
                    msgs.append(f"local log removed: {f.name}")
                except Exception as exc:
                    msgs.append(str(exc))
    except Exception as exc:
        msgs.append(str(exc))

    try:
        lock = Path(os.environ.get("TEMP", str(Path.home()))) / "GalaxyBudsMultipointFix.lock"
        if lock.exists():
            lock.unlink(missing_ok=True)
    except Exception:
        pass

    import tempfile, glob
    for pat in [str(Path(tempfile.gettempdir()) / "budsmp-wake-*.wav")]:
        for f in glob.glob(pat):
            try:
                Path(f).unlink(missing_ok=True)
                msgs.append(f"temp removed: {Path(f).name}")
            except Exception:
                pass

    start_menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{STARTUP_BASENAME}.lnk"
    try:
        if start_menu.exists():
            start_menu.unlink()
            msgs.append(f"Start Menu item removed: {start_menu}")
    except Exception as exc:
        msgs.append(str(exc))

    msgs.append("Uninstall complete — autorun + logs fully removed. Restart if needed to hide tray.")
    return msgs
