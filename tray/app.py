#!/usr/bin/env python3
"""Entrypoint — tray-first. Double-click or `uv run app.py` shows the system tray immediately.

All control lives in the tray (right-click):
  Open / Check Buds status / Run fix now / Revert fix / Enable/Disable Autorun / Uninstall / Close

CLI flags are for debugging only; normal use needs no CLI.
  uv run app.py                 # start tray
  uv run app.py --minimized     # start hidden (for Task Scheduler / exe)

Based on https://github.com/id6917824/galaxy-buds-multipoint (MIT)
by id6917824 — original reverse-engineering and fix logic (frame/transport/discover).
Windows tray extension by SvenS7 — tray/monitor/autorun/uninstall.
This tray is Windows UI/autorun only and reuses budsmp/* unchanged.
"""

from __future__ import annotations

import sys
import os
import json
import argparse
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Logging setup — local + AppData
# ---------------------------------------------------------------------------

def _appdata_logs_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    d = base / "GalaxyBudsMultipoint" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d

def setup_logging(verbose: bool = False) -> Path:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    # avoid duplicate handlers on re-import / repeated setup_logging calls
    if root.handlers:
        for h in list(root.handlers):
            # keep existing handlers but update level
            h.setLevel(level)
        root.setLevel(level)
        # find existing file log path if any
        for h in root.handlers:
            if isinstance(h, RotatingFileHandler):
                try:
                    return Path(h.baseFilename)
                except Exception:
                    pass
        return _appdata_logs_dir() / "app.log"
    root.setLevel(level)
    # console
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # file: AppData
    try:
        log_path = _appdata_logs_dir() / "app.log"
        fh = RotatingFileHandler(str(log_path), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as exc:
        logging.getLogger("app").warning("could not open AppData log: %s", exc)
        log_path = Path(__file__).resolve().parent / "logs" / "app.log"

    # file: local logs/
    try:
        local = Path(__file__).resolve().parent / "logs" / "app.log"
        local.parent.mkdir(parents=True, exist_ok=True)
        fh2 = RotatingFileHandler(str(local), maxBytes=500_000, backupCount=2, encoding="utf-8")
        fh2.setLevel(level)
        fh2.setFormatter(fmt)
        root.addHandler(fh2)
    except Exception:
        pass

    logging.getLogger("app").info("logging to %s", log_path)
    return log_path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config.json (next to app.py, then AppData override)."""
    cfg: dict = {}
    candidates = [
        Path(__file__).resolve().parent / "config.json",
        _appdata_logs_dir().parent / "config.json",
        Path(os.environ.get("APPDATA", "")) / "GalaxyBudsMultipoint" / "config.json",
    ]
    for p in candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                cfg.update(data)
                logging.getLogger("app").info("config loaded: %s", p)
            except Exception as exc:
                logging.getLogger("app").warning("could not read %s: %s", p, exc)
    # defaults
    cfg.setdefault("name_needle", "buds")
    cfg.setdefault("address", None)
    cfg.setdefault("channel", None)
    cfg.setdefault("as_ver", 2)
    cfg.setdefault("poll_interval", 3.0)
    cfg.setdefault("debounce_seconds", 20.0)
    cfg.setdefault("verify_interval", 90.0)
    cfg.setdefault("disconnected_retry_seconds", 25.0)
    cfg.setdefault("auto_apply", True)
    cfg.setdefault("listen_seconds", 6.0)
    cfg.setdefault("attempts", 8)
    cfg.setdefault("retry_delay", 0.7)
    cfg.setdefault("minimized", True)
    return cfg

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # Tray is primary; CLI is debug/advanced only. Help shows minimal info.
    p = argparse.ArgumentParser(description="Galaxy Buds Multipoint Fix — tray app (control via system tray, right-click icon)")
    p.add_argument("--minimized", action="store_true", help="start tray hidden (for autorun/exe)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    # Advanced / debug (optional, not needed for normal use):
    p.add_argument("--install", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--uninstall", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--status", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-autostart", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--name", dest="name_needle", default=None, help=argparse.SUPPRESS)
    p.add_argument("--addr", dest="address", default=None, help=argparse.SUPPRESS)
    p.add_argument("--channel", type=int, default=None, help=argparse.SUPPRESS)
    return p

def do_status(config: dict):
    import buds_fix
    st = buds_fix.check_status(
        name_needle=config.get("name_needle", "buds"),
        address=config.get("address"),
        channel=config.get("channel"),
        as_ver_probe=int(config.get("as_ver", 2)),
    )
    print(f"Paired: {st.paired}")
    print(f"Connected: {st.connected}")
    print(f"Address: {st.address or '—'}  Name: {st.name or '—'}")
    if st.as_ver is not None:
        print(f"asVer: {st.as_ver}  multipoint: {'allowed' if st.multipoint_allowed else 'blocked'}")
    else:
        print("asVer: unknown (no verify — buds sleeping or no state frame)")
    if st.last_error:
        print(f"Note: {st.last_error}")
    return 0 if st.paired else 2

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # single-instance guard — prevent duplicate tray on double-click/autorun race
    lock_path = Path(os.environ.get("TEMP", str(Path.home()))) / "GalaxyBudsMultipointFix.lock"
    def _cleanup_lock():
        try:
            if lock_path.exists():
                try:
                    if int(lock_path.read_text(encoding="utf-8").strip()) == os.getpid():
                        lock_path.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass
    import atexit
    atexit.register(_cleanup_lock)
    try:
        if lock_path.exists():
            try:
                pid = int(lock_path.read_text(encoding="utf-8").strip())
                import subprocess as _sp
                import re
                rc = _sp.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=5)
                # tasklist prints PID as exact number; use word boundary to avoid substring false positives
                # and verify the PID actually exists in output
                if re.search(rf"\b{pid}\b", rc.stdout) and "INFO:" not in rc.stdout:
                    print(f"App already running (PID {pid}) — focus tray instead of second instance.", file=sys.stderr)
                    sys.exit(0)
                else:
                    # stale lock (PID not running or PID reuse but task not found)
                    try:
                        lock_path.unlink(missing_ok=True)
                    except Exception:
                        pass
            except ValueError:
                # corrupt lock file
                try:
                    lock_path.unlink(missing_ok=True)
                except Exception:
                    pass
            except SystemExit:
                raise
            except Exception:
                pass
        # atomic-ish create: write PID
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
    except SystemExit:
        raise
    except Exception:
        pass

    setup_logging(verbose=args.verbose)
    # When --minimized: keep console quiet (file logs only), tray remains primary
    if args.minimized:
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)
    log = logging.getLogger("app")

    # Config overrides via CLI
    config = load_config()
    if args.name_needle:
        config["name_needle"] = args.name_needle
    if args.address:
        config["address"] = args.address
    if args.channel is not None:
        config["channel"] = args.channel
    if args.minimized:
        config["minimized"] = True

    # --uninstall
    if args.uninstall:
        import startup
        log.info("uninstall requested")
        msgs = startup.uninstall_all()
        for m in msgs:
            print(m)
            log.info(m)
        try:
            from tkinter import Tk, messagebox
            r = Tk(); r.withdraw()
            messagebox.showinfo("Uninstall complete", "\n".join(msgs))
            r.destroy()
        except Exception:
            pass
        return 0

    # --install
    if args.install:
        import startup
        log.info("install requested")
        msgs = startup.install_autorun()
        for m in msgs:
            print(m)
            log.info(m)
        try:
            from tkinter import Tk, messagebox
            r = Tk(); r.withdraw()
            if startup.task_exists():
                messagebox.showinfo("Install complete",
                                    "Autorun installed:\n" + "\n".join(msgs) +
                                    "\n\nThe app will start automatically at next login.")
            else:
                messagebox.showwarning("Install",
                                       "Task Scheduler failed, fallback used:\n" + "\n".join(msgs))
            r.destroy()
        except Exception:
            pass
            return do_status(config)
        # --install without --status should exit after installing
        return 0

    if args.status:
        return do_status(config)

    # Autorun is NOT installed automatically — user controls via tray:
    #   Tray > Enable Autorun / Disable Autorun
    # Only log current status.
    try:
        import startup
        if startup.is_installed():
            log.info("autorun: on (Task Scheduler / Startup)")
        else:
            log.info("autorun: off — enable via tray > Enable Autorun")
    except Exception:
        pass

    # Hide console window when --minimized (Windows)
    if args.minimized and sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass

    # Start monitor + tray
    import buds_fix
    monitor = buds_fix.BudsMonitor(config)
    monitor.start()
    log.info("monitor started (poll %.1fs debounce %.1fs)", config["poll_interval"], config["debounce_seconds"])

    # Tray (blocks until Exit)
    exit_code = 0
    try:
        import tray_app
        app = tray_app.TrayApp(config, monitor)
        app.run()
    except Exception as exc:
        log.exception("tray crash: %s", exc)
        print(f"tray could not start ({exc}) — exiting.", file=sys.stderr)
        try:
            from tkinter import Tk, messagebox
            r = Tk(); r.withdraw()
            messagebox.showerror("Galaxy Buds Multipoint Fix", f"Tray failed to start:\n{exc}\n\nCheck logs for details.")
            r.destroy()
        except Exception:
            pass
        exit_code = 1
    finally:
        monitor.stop()
        log.info("app closed")
        _cleanup_lock()

    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
