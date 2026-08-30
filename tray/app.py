#!/usr/bin/env python3
"""Entrypoint — tray-first. Dubbelklik of `uv run app.py` toont direct de systeemtray.

Alle bediening zit in de tray (rechtsklik):
  Open / Check Buds status / Run fix now / Revert fix / Autorun aan/uit / Uninstall / Sluiten

CLI-flags zijn alleen voor debug; normaal gebruik heeft geen CLI nodig.
  uv run app.py                 # start tray
  uv run app.py --minimized     # start verborgen (voor Task Scheduler / exe)
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
# Logging setup — lokaal + AppData
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
        logging.getLogger("app").warning("kon AppData log niet openen: %s", exc)
        log_path = Path(__file__).resolve().parent / "logs" / "app.log"

    # file: lokale logs/
    try:
        local = Path(__file__).resolve().parent / "logs" / "app.log"
        local.parent.mkdir(parents=True, exist_ok=True)
        fh2 = RotatingFileHandler(str(local), maxBytes=500_000, backupCount=2, encoding="utf-8")
        fh2.setLevel(level)
        fh2.setFormatter(fmt)
        root.addHandler(fh2)
    except Exception:
        pass

    logging.getLogger("app").info("logging naar %s", log_path)
    return log_path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Laad config.json (naast app.py, dan AppData override)."""
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
                logging.getLogger("app").info("config geladen: %s", p)
            except Exception as exc:
                logging.getLogger("app").warning("kon %s niet lezen: %s", p, exc)
    # defaults
    cfg.setdefault("name_needle", "buds")
    cfg.setdefault("address", None)
    cfg.setdefault("channel", None)
    cfg.setdefault("as_ver", 2)
    cfg.setdefault("poll_interval", 3.0)
    cfg.setdefault("debounce_seconds", 20.0)
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
    # Tray is primair; CLI is alleen debug/advanced. Help toont daarom minimale uitleg.
    p = argparse.ArgumentParser(description="Galaxy Buds Multipoint Fix — tray app (bediening via systeemtray, rechtsklik icoon)")
    p.add_argument("--minimized", action="store_true", help="start tray verborgen (voor autorun/exe)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    # Geavanceerd / debug (optioneel, niet nodig voor normaal gebruik):
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
        print(f"asVer: {st.as_ver}  multipoint: {'toegestaan' if st.multipoint_allowed else 'geblokkeerd'}")
    else:
        print("asVer: onbekend (geen verify — buds slapen of geen state frame)")
    if st.last_error:
        print(f"Opmerking: {st.last_error}")
    return 0 if st.paired else 2

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose)
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
        log.info("uninstall aangevraagd")
        msgs = startup.uninstall_all()
        for m in msgs:
            print(m)
            log.info(m)
        # Probeer ook GUI dialoog indien mogelijk
        try:
            from tkinter import Tk, messagebox
            r = Tk(); r.withdraw()
            messagebox.showinfo("Uninstall voltooid", "\n".join(msgs))
            r.destroy()
        except Exception:
            pass
        return 0

    # --install
    if args.install:
        import startup
        log.info("install aangevraagd")
        msgs = startup.install_autorun()
        for m in msgs:
            print(m)
            log.info(m)
        try:
            from tkinter import Tk, messagebox
            r = Tk(); r.withdraw()
            # check of taak bestaat
            if startup.task_exists():
                messagebox.showinfo("Installatie voltooid",
                                    "Autorun geïnstalleerd:\n" + "\n".join(msgs) +
                                    "\n\nDe app start automatisch bij volgende login.")
            else:
                messagebox.showwarning("Installatie",
                                       "Task Scheduler mislukt, fallback gebruikt:\n" + "\n".join(msgs))
            r.destroy()
        except Exception:
            pass
        # na install meteen tray starten tenzij --status
        if args.status:
            return do_status(config)

    if args.status:
        return do_status(config)

    # Autorun wordt NIET automatisch geïnstalleerd — gebruiker beheert dit via tray:
    #   Tray > Autorun inschakelen / Autorun uitschakelen
    # Alleen loggen wat de huidige status is.
    try:
        import startup
        if startup.is_installed():
            log.info("autorun: aan (Task Scheduler / Startup)")
        else:
            log.info("autorun: uit — inschakelen via tray > Autorun inschakelen")
    except Exception:
        pass

    # Verberg console window bij --minimized (Windows)
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
    log.info("monitor gestart (poll %.1fs debounce %.1fs)", config["poll_interval"], config["debounce_seconds"])

    # Tray (blokkeert tot Exit)
    try:
        import tray_app
        app = tray_app.TrayApp(config, monitor)
        app.run()
    except Exception as exc:
        log.exception("tray crash: %s", exc)
        # Fallback: blijf monitor draaien zonder tray (voor headless)
        print(f"tray kon niet starten ({exc}) — monitor blijft actief. Ctrl+C om te stoppen.", file=sys.stderr)
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        monitor.stop()
        log.info("app afgesloten")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
