"""Systeemtray-icoon, menu en klein statusvenster (tkinter + pystray)."""

from __future__ import annotations

import threading
import logging
import queue
from pathlib import Path
from tkinter import Tk, Toplevel, Label, Button, Text, Frame, END, messagebox

log = logging.getLogger("tray")

try:
    import pystray
    from pystray import MenuItem as item
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception as exc:
    log.warning("pystray/Pillow niet beschikbaar: %s", exc)
    HAS_TRAY = False
    pystray = None
    item = None
    Image = None

import buds_fix
import startup

APP_NAME = "Galaxy Buds Multipoint Fix"


def _make_icon_image(size: int = 64):
    """Genereer icoon: blauwe cirkel met 'B' (voorkomt externe .ico dependency)."""
    if Image is None:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Blauwe achtergrond
    draw.ellipse([2, 2, size - 3, size - 3], fill="#1a73e8", outline="#0d47a1", width=2)
    # Witte B (simpel)
    # Probeer font, fallback naar polygon
    try:
        # kleine B via lijnen
        cx, cy = size // 2, size // 2
        draw.text((cx, cy), "B", fill="white", anchor="mm", font=None)
    except Exception:
        pass
    # Als text niet zichtbaar, teken twee cirkels als "buds"
    if size >= 48:
        # voeg kleine accenten toe
        draw.ellipse([size // 3, size // 2 - 6, size // 3 + 10, size // 2 + 6], fill="white")
        draw.ellipse([2 * size // 3 - 10, size // 2 - 6, 2 * size // 3, size // 2 + 6], fill="white")
    return img


class StatusWindow:
    """Klein statusvenster (tkinter Toplevel)."""

    def __init__(self, root: Tk, config: dict, get_status_fn):
        self.root = root
        self.config = config
        self.get_status_fn = get_status_fn
        self.win: Toplevel | None = None
        self.labels: dict[str, Label] = {}
        self.log_text: Text | None = None
        self._after_id = None
        self._status_cache = "—"
        self._build_hidden()

    def _build_hidden(self):
        # root is hidden; window wordt on-demand getoond
        pass

    def show(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            return
        self.win = Toplevel(self.root)
        self.win.title(APP_NAME + " — Status")
        self.win.geometry("420x380")
        self.win.resizable(False, False)
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass
        self.win.protocol("WM_DELETE_WINDOW", self.hide)

        # Content
        pad = 12
        frm = Frame(self.win)
        frm.pack(fill="both", expand=True, padx=pad, pady=pad)

        title = Label(frm, text=APP_NAME, font=("Segoe UI", 11, "bold"))
        title.pack(anchor="w")

        self.labels["paired"] = Label(frm, text="Gekoppeld: …", anchor="w", justify="left")
        self.labels["connected"] = Label(frm, text="Verbonden: …", anchor="w")
        self.labels["fix"] = Label(frm, text="Fix: …", anchor="w")
        self.labels["addr"] = Label(frm, text="Adres: …", anchor="w", font=("Consolas", 8))
        self.labels["autorun"] = Label(frm, text="Autorun: …", anchor="w", fg="#1a73e8")
        self.labels["error"] = Label(frm, text="", anchor="w", fg="#b00020", wraplength=380, justify="left")
        for k in ["paired", "connected", "fix", "addr", "autorun", "error"]:
            self.labels[k].pack(anchor="w", pady=2)

        btn_frame = Frame(frm)
        btn_frame.pack(fill="x", pady=(8, 4))
        Button(btn_frame, text="Check Buds status", command=self.refresh_now).pack(side="left", padx=2)
        Button(btn_frame, text="Run fix now", command=self._run_fix).pack(side="left", padx=2)
        Button(btn_frame, text="Revert fix", command=self._revert_fix).pack(side="left", padx=2)
        Button(btn_frame, text="Sluiten", command=self.hide).pack(side="right", padx=2)

        autorun_frame = Frame(frm)
        autorun_frame.pack(fill="x", pady=(4, 0))
        Button(autorun_frame, text="Autorun inschakelen", command=self._autorun_enable).pack(side="left", padx=2)
        Button(autorun_frame, text="Autorun uitschakelen", command=self._autorun_disable).pack(side="left", padx=2)

        Label(frm, text="Log (laatste events):", font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 0))
        self.log_text = Text(frm, height=8, font=("Consolas", 7), wrap="word")
        self.log_text.pack(fill="both", expand=False)
        self.log_text.insert(END, "Statusvenster geopend.\n")
        self.log_text.configure(state="disabled")

        self.refresh_now()
        self._schedule_refresh()

    def hide(self):
        if self.win is not None:
            try:
                self.win.withdraw()
            except Exception:
                pass
            if self._after_id:
                try:
                    self.win.after_cancel(self._after_id)
                except Exception:
                    pass
                self._after_id = None

    def destroy(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None

    def _schedule_refresh(self):
        if self.win is None or not self.win.winfo_exists():
            return
        # Alleen als venster zichtbaar is, en met rustig interval (voorkomt RFCOMM storm met monitor)
        self._after_id = self.win.after(15000, self._auto_refresh)

    def _auto_refresh(self):
        # Silent auto-refresh alleen autorun/pairstatus, geen RFCOMM storm
        if self.win is None or not self.win.winfo_exists():
            return
        # Alleen lichte refresh: autorun label updaten, geen zware RFCOMM probe
        try:
            autorun_on = startup.is_installed()
            self.labels["autorun"].config(text=f"Autorun: {'aan' if autorun_on else 'uit'} (start bij login)")
        except Exception:
            pass
        self._schedule_refresh()

    def refresh_now(self, silent: bool = False):
        # Alleen expliciete user-actie (knop of tray) doet zware RFCOMM check_status;
        # silent auto-refresh doet dat niet meer om 10048 te voorkomen
        if silent:
            # lichte silent path reeds in _auto_refresh — hier niets doen
            return
        self.labels["fix"].config(text="Fix: controleren…")
        self.win.update_idletasks()

        def worker():
            try:
                st = self.get_status_fn()
                self.root.after(0, lambda: self._apply_status(st))
            except Exception as exc:
                self.root.after(0, lambda: self._apply_error(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_status(self, st):
        if self.win is None or not self.win.winfo_exists():
            return
        self.labels["paired"].config(text=f"Gekoppeld: {'ja' if st.paired else 'nee'}")
        self.labels["connected"].config(text=f"Verbonden: {'ja' if st.connected else 'nee'}")
        if st.as_ver is None:
            self.labels["fix"].config(text="Fix: onbekend (buds slapen of geen verify)")
        else:
            ok = "toegestaan" if st.multipoint_allowed else "geblokkeerd"
            self.labels["fix"].config(text=f"Fix: asVer={st.as_ver} — multipoint {ok}")
        self.labels["addr"].config(text=f"Adres: {st.address or '—'}  \"{st.name or ''}\"")
        # autorun status live
        try:
            autorun_on = startup.is_installed()
            self.labels["autorun"].config(text=f"Autorun: {'aan' if autorun_on else 'uit'} (start bij login)")
        except Exception:
            pass
        self.labels["error"].config(text=st.last_error or "")
        if st.last_error:
            self.append_log(f"[status] {st.last_error}")

    def _autorun_enable(self):
        self.append_log("Autorun inschakelen…")
        def worker():
            try:
                msgs = startup.install_autorun()
                self.root.after(0, lambda: [self.append_log(m) for m in msgs])
                self.root.after(0, lambda: messagebox.showinfo("Autorun", "\n".join(msgs)) if self.win and self.win.winfo_exists() else None)
                self.root.after(0, lambda: self.refresh_now(silent=True))
            except Exception as exc:
                self.root.after(0, lambda: self.append_log(f"Autorun fout: {exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _autorun_disable(self):
        self.append_log("Autorun uitschakelen…")
        def worker():
            try:
                msgs = startup.uninstall_autorun()
                self.root.after(0, lambda: [self.append_log(m) for m in msgs])
                self.root.after(0, lambda: messagebox.showinfo("Autorun", "\n".join(msgs)) if self.win and self.win.winfo_exists() else None)
                self.root.after(0, lambda: self.refresh_now(silent=True))
            except Exception as exc:
                self.root.after(0, lambda: self.append_log(f"Autorun fout: {exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_error(self, msg: str):
        if self.win is None or not self.win.winfo_exists():
            return
        self.labels["error"].config(text=msg)

    def append_log(self, msg: str):
        if self.log_text is None or self.win is None or not self.win.winfo_exists():
            return
        self.log_text.configure(state="normal")
        self.log_text.insert(END, msg + "\n")
        self.log_text.see(END)
        # keep last ~200 lines
        lines = self.log_text.get("1.0", END).splitlines()
        if len(lines) > 200:
            self.log_text.delete("1.0", f"{len(lines)-200}.0")
        self.log_text.configure(state="disabled")

    def _run_fix(self):
        self.append_log("Run fix now…")
        self.labels["fix"].config(text="Fix: wordt toegepast…")
        def worker():
            try:
                dev = buds_fix.find_buds(self.config.get("name_needle", "buds"),
                                         self.config.get("address"))
                if not dev:
                    self.root.after(0, lambda: self.append_log("Geen Buds gevonden"))
                    return
                ch = self.config.get("channel")
                as_ver = int(self.config.get("as_ver", 2))
                ok, msg = buds_fix.apply_fix(dev.address, ch, as_ver=as_ver)
                self.root.after(0, lambda: self.append_log(f"Fix {'ok' if ok else 'mislukt'}: {msg}"))
                self.root.after(0, lambda: self.refresh_now(silent=True))
            except Exception as exc:
                self.root.after(0, lambda: self.append_log(f"Fout: {exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _revert_fix(self):
        if not messagebox.askyesno("Revert fix", "Multipoint-fix ongedaan maken (asVer=0)?\n\nDe Buds vallen dan weer terug op stock gedrag."):
            return
        self.append_log("Revert…")
        def worker():
            try:
                dev = buds_fix.find_buds(self.config.get("name_needle", "buds"),
                                         self.config.get("address"))
                if not dev:
                    self.root.after(0, lambda: self.append_log("Geen Buds gevonden"))
                    return
                ch = self.config.get("channel")
                ok, msg = buds_fix.revert_fix(dev.address, ch)
                self.root.after(0, lambda: self.append_log(f"Revert {'ok' if ok else 'mislukt'}: {msg}"))
                self.root.after(0, lambda: self.refresh_now(silent=True))
            except Exception as exc:
                self.root.after(0, lambda: self.append_log(f"Fout: {exc}"))
        threading.Thread(target=worker, daemon=True).start()


class TrayApp:
    def __init__(self, config: dict, monitor: buds_fix.BudsMonitor | None = None):
        self.config = config
        self.monitor = monitor
        self.root: Tk | None = None
        self.status_win: StatusWindow | None = None
        self.icon = None
        self._event_q: queue.Queue[str] = queue.Queue()
        self._status_q: queue.Queue[buds_fix.BudsStatus] = queue.Queue()

    def _get_status(self):
        return buds_fix.check_status(
            name_needle=self.config.get("name_needle", "buds"),
            address=self.config.get("address"),
            channel=self.config.get("channel"),
            as_ver_probe=int(self.config.get("as_ver", 2)),
            listen_seconds=float(self.config.get("listen_seconds", 6.0)),
            attempts=int(self.config.get("attempts", 8)),
            retry_delay=float(self.config.get("retry_delay", 0.7)),
        )

    # -- tray callbacks -----------------------------------------------------

    def _on_open(self, icon, item):
        if self.root and self.status_win:
            self.root.after(0, self.status_win.show)

    def _on_check(self, icon, item):
        # Doe check in thread, toon daarna window
        if self.root and self.status_win:
            self.root.after(0, self.status_win.show)
            self.root.after(100, self.status_win.refresh_now)

    def _on_fix(self, icon, item):
        def worker():
            dev = buds_fix.find_buds(self.config.get("name_needle", "buds"),
                                     self.config.get("address"))
            if not dev:
                self._notify("Geen Buds gevonden", "Koppel eerst Galaxy Buds in Bluetooth-instellingen")
                return
            ok, msg = buds_fix.apply_fix(dev.address, self.config.get("channel"),
                                         as_ver=int(self.config.get("as_ver", 2)))
            self._notify("Fix " + ("ok" if ok else "mislukt"), msg)
            if self.status_win:
                self.root.after(0, lambda: self.status_win.append_log(f"[fix] {msg}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_revert(self, icon, item):
        def worker():
            dev = buds_fix.find_buds(self.config.get("name_needle", "buds"),
                                     self.config.get("address"))
            if not dev:
                self._notify("Geen Buds gevonden", "")
                return
            ok, msg = buds_fix.revert_fix(dev.address, self.config.get("channel"))
            self._notify("Revert " + ("ok" if ok else "mislukt"), msg)
        threading.Thread(target=worker, daemon=True).start()

    def _on_autorun_enable(self, icon, item):
        def do_ui():
            msgs = startup.install_autorun()
            # tray balloon + dialoog via tkinter
            self._notify("Autorun ingeschakeld", msgs[0] if msgs else "Autorun aan")
            if self.root:
                self.root.after(0, lambda: messagebox.showinfo("Autorun ingeschakeld", "\n".join(msgs)))
            if self.status_win:
                for m in msgs:
                    self._event_q.put(m)
            self._refresh_menu()
        # pystray callback loopt niet op Tk thread; schakel naar Tk voor dialog
        if self.root:
            self.root.after(0, do_ui)
        else:
            do_ui()

    def _on_autorun_disable(self, icon, item):
        def do_ui():
            msgs = startup.uninstall_autorun()
            self._notify("Autorun uitgeschakeld", msgs[0] if msgs else "Autorun uit")
            if self.root:
                self.root.after(0, lambda: messagebox.showinfo("Autorun uitgeschakeld", "\n".join(msgs)))
            if self.status_win:
                for m in msgs:
                    self._event_q.put(m)
            self._refresh_menu()
        if self.root:
            self.root.after(0, do_ui)
        else:
            do_ui()

    def _on_uninstall(self, icon, item):
        # Vraag bevestiging via tkinter
        def do_ui():
            if not messagebox.askyesno("Uninstall",
                                       "Volledig verwijderen en app sluiten?\n\n"
                                       "Dit verwijdert:\n"
                                       "• Task Scheduler taak\n"
                                       "• Startup items & Registry key\n"
                                       "• Logs en temp bestanden\n\n"
                                       "De app zelf (exe/py) blijft staan — handmatig verwijderen indien gewenst."):
                return
            msgs = startup.uninstall_all()
            messagebox.showinfo("Uninstall voltooid", "\n".join(msgs))
            self._on_exit(icon, item)
        if self.root:
            self.root.after(0, do_ui)

    def _on_exit(self, icon, item):
        log.info("Exit via tray menu")
        if self.monitor:
            self.monitor.stop()
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self.root:
            try:
                self.root.after(0, self.root.quit)
            except Exception:
                pass

    def _notify(self, title: str, msg: str):
        if self.icon:
            try:
                self.icon.notify(msg, title)
            except Exception:
                log.info("%s: %s", title, msg)
        else:
            log.info("%s: %s", title, msg)

    def _is_autorun_on(self, item):
        try:
            return startup.is_installed()
        except Exception:
            return False

    def _is_autorun_off(self, item):
        try:
            return not startup.is_installed()
        except Exception:
            return True

    def _refresh_menu(self):
        if self.icon:
            try:
                self.icon.menu = self._build_menu()
                self.icon.update_menu()
            except Exception:
                pass

    def _build_menu(self):
        return pystray.Menu(
            item("Open", self._on_open, default=True),
            item("Check Buds status", self._on_check),
            item("Run fix now", self._on_fix),
            item("Revert fix", self._on_revert),
            pystray.Menu.SEPARATOR,
            item("Autorun inschakelen", self._on_autorun_enable, enabled=lambda i: self._is_autorun_off(i)),
            item("Autorun uitschakelen", self._on_autorun_disable, enabled=lambda i: self._is_autorun_on(i)),
            pystray.Menu.SEPARATOR,
            item("Uninstall (volledig verwijderen)", self._on_uninstall),
            item("Sluiten", self._on_exit),
        )

    def _poll_queues(self):
        # Wordt via Tk after gepolld; toont monitor events in statusvenster
        try:
            while True:
                msg = self._event_q.get_nowait()
                if self.status_win:
                    self.status_win.append_log(msg)
                # ook als balloon
                # self._notify(APP_NAME, msg)
        except queue.Empty:
            pass
        try:
            while True:
                st = self._status_q.get_nowait()
                # update tooltip
                if self.icon:
                    tip = APP_NAME
                    if not st.paired:
                        tip += " — geen Buds gekoppeld"
                    elif not st.connected:
                        tip += f" — {st.name or 'Buds'} niet verbonden"
                    else:
                        tip += f" — {st.name or 'Buds'} verbonden"
                    try:
                        self.icon.title = tip
                    except Exception:
                        pass
        except queue.Empty:
            pass
        if self.root:
            self.root.after(1000, self._poll_queues)

    def run(self):
        # Tk root (hidden)
        self.root = Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        # Verberg console op Windows indien mogelijk
        try:
            self.root.attributes("-alpha", 0.0)
            self.root.after(100, lambda: self.root.attributes("-alpha", 1.0))
        except Exception:
            pass

        self.status_win = StatusWindow(self.root, self.config, self._get_status)

        # Wiring monitor callbacks -> queues (threadsafe)
        if self.monitor:
            orig_event = self.monitor.on_event
            orig_status = self.monitor.on_status
            def on_event(msg: str):
                self._event_q.put(msg)
                if orig_event:
                    try: orig_event(msg)
                    except Exception: pass
            def on_status(st):
                self._status_q.put(st)
                if orig_status:
                    try: orig_status(st)
                    except Exception: pass
            self.monitor.on_event = on_event
            self.monitor.on_status = on_status

        self.root.after(1000, self._poll_queues)

        if not HAS_TRAY:
            log.warning("pystray niet beschikbaar — val terug op alleen statusvenster")
            self.status_win.show()
            self.root.deiconify()
            self.root.mainloop()
            return

        img = _make_icon_image(64)
        # Toon venster bij eerste start indien niet minimized
        if not self.config.get("minimized", True):
            self.root.after(500, self.status_win.show)

        self.icon = pystray.Icon(APP_NAME, img, APP_NAME, self._build_menu())

        # pystray icon.run blokkeert; draai Tk mainloop in aparte thread
        # Maar pystray op Windows moet in main thread. Dus we draaien icon in thread en Tk in main.
        # Alternatief: icon.run_detached — we doen hybride: Tk after + icon in daemon thread.
        def run_icon():
            try:
                self.icon.run()
            except Exception as exc:
                log.exception("tray icon error: %s", exc)

        t = threading.Thread(target=run_icon, daemon=True, name="tray-icon")
        t.start()

        # Ook bij geen tray: blijf Tk draaien
        log.info("tray gestart — rechtsklik icoon voor menu")
        self.root.mainloop()

        # Na mainloop: cleanup
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self.monitor:
            self.monitor.stop()
