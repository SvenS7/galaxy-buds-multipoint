"""Buds-detectie, connect-detectie en fix-logica.

Hergebruikt bestaande budsmp primitives (frame/transport/discover) zonder
te forken. Volledig lokaal, stdlib + budsmp.
"""

from __future__ import annotations

import time
import threading
import logging
from dataclasses import dataclass
from typing import Callable

from budsmp import frame, discover, transport

log = logging.getLogger("buds_fix")

SPP4_FALLBACK_CHANNEL = 29


@dataclass
class BudsStatus:
    paired: bool
    connected: bool
    address: str | None
    name: str | None
    as_ver: int | None
    multipoint_allowed: bool | None
    last_error: str | None = None


def _null_log(msg: str = "") -> None:
    log.debug(msg)


def find_buds(name_needle: str = "buds", address: str | None = None):
    """Vind het doel-device (zelfde resolutie als cli.py)."""
    # discover.find_device verwacht een log callable
    dev = discover.find_device(address, name_needle, _null_log)
    return dev


def paired_buds(name_needle: str = "buds") -> list:
    """Alle gepaarde devices waarvan naam needle bevat."""
    all_devs = discover.paired_devices()
    needle = name_needle.lower()
    return [d for d in all_devs if needle in d.name.lower()]


def resolve_channel(address: str, override: int | None) -> int:
    return discover.resolve_channel(address, override, _null_log)


def _do_rfcomm_write(address: str, channel: int, as_ver: int,
                     attempts: int, retry_delay: float,
                     listen_seconds: float) -> tuple[bool, str, int | None]:
    """Voer één RFCOMM write uit. Returns (ok, msg, reported_as_ver)."""
    try:
        transport.bluetooth_family()
    except transport.Unsupported as exc:
        return False, str(exc), None

    fr = frame.version_only(as_ver)
    log.info("RFCOMM open %s ch%d asVer=%d", address, channel, as_ver)
    sess = transport.Session(address, channel, _null_log,
                             attempts=attempts, retry_delay=retry_delay)
    try:
        sess.open()
    except transport.OpenFailed as exc:
        return False, f"RFCOMM open failed: {exc}", None
    except Exception as exc:
        return False, f"open error: {exc}", None

    try:
        sess.send([fr])
        rx = sess.listen(listen_seconds)
    finally:
        sess.close()

    states = frame.decode_state(rx)
    if not states:
        # Write kan gelukt zijn maar zonder NOTIFY (buds re-evalueren alleen bij audio-change)
        # Beschouw als half-ok: verzonden maar niet geverifieerd
        return True, "verzonden (geen state frame — start/stop playback en check opnieuw)", None

    # laatste frame is huidige state (cli.py:229)
    reported = states[-1].as_ver
    allowed = frame.multipoint_allowed(reported)
    # firmware normaliseert 0 -> 1
    if reported == as_ver or (as_ver == 0 and reported <= 1):
        return True, f"asVer={reported} ({'multipoint toegestaan' if allowed else 'geblokkeerd'})", reported
    # asVer mismatch maar write is wel verzonden
    if len({s.as_ver for s in states}) > 1:
        first = states[0].as_ver
        return False, f"wrote {as_ver} maar device reports {reported} (was {first})", reported
    return False, f"wrote {as_ver} maar device reports {reported}", reported


def apply_fix(address: str, channel: int | None = None,
              as_ver: int = 2, attempts: int = 8,
              retry_delay: float = 0.7, listen_seconds: float = 6.0) -> tuple[bool, str]:
    ch = resolve_channel(address, channel)
    ok, msg, _ = _do_rfcomm_write(address, ch, as_ver, attempts, retry_delay, listen_seconds)
    # voor revert/apply is mismatch een "niet geverifieerd" maar wel verzonden
    # apply: alleen fout als open faalde
    return ok, msg


def revert_fix(address: str, channel: int | None = None,
               attempts: int = 8, retry_delay: float = 0.7,
               listen_seconds: float = 6.0) -> tuple[bool, str]:
    return apply_fix(address, channel, as_ver=0, attempts=attempts,
                     retry_delay=retry_delay, listen_seconds=listen_seconds)


def check_status(name_needle: str = "buds", address: str | None = None,
                 channel: int | None = None,
                 as_ver_probe: int = 2,
                 listen_seconds: float = 6.0,
                 attempts: int = 8, retry_delay: float = 0.7) -> BudsStatus:
    """Rapporteer pairing, connection en vermoedelijke fix-status.

    Doet een read-achtige probe: schrijft as_ver_probe opnieuw (no-op) om
    een state NOTIFY uit te lokken, zoals cli `read` doet.
    """
    dev = find_buds(name_needle, address)
    if dev is None:
        # Geen match -> check of er überhaupt iets gepaired is
        all_devs = discover.paired_devices()
        if not all_devs:
            return BudsStatus(False, False, None, None, None, None, "geen gepaarde devices gevonden")
        return BudsStatus(False, False, None, None, None, None,
                          f'geen gepaard device met "{name_needle}" (scan toont {len(all_devs)} device(s))')

    ch = resolve_channel(dev.address, channel)
    # Probe write om state te krijgen
    ok, msg, reported = _do_rfcomm_write(dev.address, ch, as_ver_probe,
                                         attempts=attempts, retry_delay=retry_delay,
                                         listen_seconds=listen_seconds)
    if reported is not None:
        return BudsStatus(True, dev.connected, dev.address, dev.name,
                          reported, frame.multipoint_allowed(reported), None)
    # Geen state terug -> alleen pairing/connected bekend
    # Als open faalde, is last_error relevant
    if not ok and "open failed" in msg.lower():
        return BudsStatus(True, dev.connected, dev.address, dev.name, None, None, msg)
    return BudsStatus(True, dev.connected, dev.address, dev.name, None, None,
                      "geen state frame (buds slapen? start playback)")


# ---------------------------------------------------------------------------
# Background monitor — pollt pairing/connection en auto-applied fix
# ---------------------------------------------------------------------------

class BudsMonitor(threading.Thread):
    """Pollt elke poll_interval; bij nieuwe connectie -> apply_fix."""

    def __init__(self, config: dict,
                 on_event: Callable[[str], None] | None = None,
                 on_status: Callable[[BudsStatus], None] | None = None):
        super().__init__(daemon=True, name="buds-monitor")
        self.config = config
        self.on_event = on_event
        self.on_status = on_status
        self._stop = threading.Event()
        self._last_connected: dict[str, bool] = {}
        self._last_fix_ts: float = 0.0
        self._last_status: BudsStatus | None = None

    def stop(self):
        self._stop.set()

    def run(self):
        name_needle = self.config.get("name_needle", "buds")
        address = self.config.get("address")
        channel = self.config.get("channel")
        poll = float(self.config.get("poll_interval", 3.0))
        debounce = float(self.config.get("debounce_seconds", 20.0))
        as_ver = int(self.config.get("as_ver", 2))
        auto_apply = bool(self.config.get("auto_apply", True))
        attempts = int(self.config.get("attempts", 8))
        retry_delay = float(self.config.get("retry_delay", 0.7))
        listen_seconds = float(self.config.get("listen_seconds", 6.0))

        log.info("monitor start: needle=%r poll=%.1fs debounce=%.1fs auto_apply=%s",
                 name_needle, poll, debounce, auto_apply)

        while not self._stop.is_set():
            try:
                # Vind candidates
                if address:
                    dev = find_buds(name_needle, address)
                    candidates = [dev] if dev else []
                else:
                    candidates = paired_buds(name_needle)

                if not candidates:
                    # Rustige status wanneer niet gekoppeld
                    if self._last_status is None or self._last_status.paired:
                        st = BudsStatus(False, False, None, None, None, None, None)
                        self._last_status = st
                        if self.on_status:
                            self.on_status(st)
                    self._last_connected.clear()
                    self._stop.wait(poll)
                    continue

                for dev in candidates:
                    addr = dev.address
                    was = self._last_connected.get(addr)
                    is_now = bool(dev.connected)
                    # Update status callback (eerste device)
                    if dev == candidates[0]:
                        st = BudsStatus(True, is_now, dev.address, dev.name, None, None, None)
                        self._last_status = st
                        if self.on_status:
                            self.on_status(st)

                    # Detecteer disconnect -> reset debounce zodat case-trip opnieuw triggert
                    if was is True and is_now is False:
                        log.info("disconnect gedetecteerd %s \"%s\"", addr, dev.name)
                        # laat debounce verlopen: volgende connect mag meteen
                        # maar voorkom meteen dubbele trigger door last_fix_ts te behouden
                        # we wissen alleen was-state
                        self._last_connected[addr] = False
                        if self.on_event:
                            self.on_event(f"Buds disconnected: {dev.name}")
                        continue

                    if was is False and is_now is True or (was is None and is_now is True):
                        # Nieuwe connectie
                        now = time.monotonic()
                        if now - self._last_fix_ts < debounce:
                            log.info("connect %s maar debounce %.0fs rest — skip",
                                     addr, debounce - (now - self._last_fix_ts))
                            self._last_connected[addr] = True
                            continue
                        log.info("connect gedetecteerd %s \"%s\" -> auto fix asVer=%d",
                                 addr, dev.name, as_ver)
                        if self.on_event:
                            self.on_event(f"Buds connected: {dev.name} — fix wordt toegepast…")
                        if auto_apply:
                            # Extra kleine delay: SPP server komt ~0.5-1s na ACL up
                            time.sleep(0.8)
                            ok, msg = apply_fix(addr, channel, as_ver=as_ver,
                                                attempts=max(attempts, 10),
                                                retry_delay=retry_delay,
                                                listen_seconds=listen_seconds)
                            self._last_fix_ts = time.monotonic()
                            level = logging.INFO if ok else logging.WARNING
                            log.log(level, "auto fix %s: %s", addr, msg)
                            if self.on_event:
                                self.on_event(f"Fix {'ok' if ok else 'mislukt'}: {msg}")
                        else:
                            log.info("auto_apply uit — skip")
                        self._last_connected[addr] = True
                    else:
                        self._last_connected[addr] = is_now

            except Exception as exc:
                log.exception("monitor loop error: %s", exc)
                if self.on_event:
                    self.on_event(f"Monitor fout: {exc} (blijft actief)")

            self._stop.wait(poll)

        log.info("monitor gestopt")
