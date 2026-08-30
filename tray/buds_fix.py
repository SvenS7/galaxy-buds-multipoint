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

# Globaal slot: de Buds hebben maar één RFCOMM SPP4 socket; gelijktijdige opens
# vanuit monitor + UI (StatusWindow) geven WSAEADDRINUSE 10048. Dit lock serialiseert alle opens.
_rfcomm_lock = threading.Lock()


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
    """Voer één RFCOMM write uit. Returns (ok, msg, reported_as_ver). Serialiseert via _rfcomm_lock."""
    # Wacht hooguit kort op lock; als UI en monitor tegelijk willen, laat één wachten
    acquired = _rfcomm_lock.acquire(timeout=30)
    if not acquired:
        return False, "RFCOMM busy (andere operatie actief)", None
    try:
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
            return True, "verzonden (geen state frame — start/stop playback en check opnieuw)", None

        reported = states[-1].as_ver
        allowed = frame.multipoint_allowed(reported)
        if reported == as_ver or (as_ver == 0 and reported <= 1):
            return True, f"asVer={reported} ({'multipoint toegestaan' if allowed else 'geblokkeerd'})", reported
        if len({s.as_ver for s in states}) > 1:
            first = states[0].as_ver
            return False, f"wrote {as_ver} maar device reports {reported} (was {first})", reported
        return False, f"wrote {as_ver} maar device reports {reported}", reported
    finally:
        _rfcomm_lock.release()


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
# Helpers voor robuuste detectie
# ---------------------------------------------------------------------------

def _can_open_rfcomm(address: str, channel: int | None, attempts: int = 2,
                     retry_delay: float = 0.4, timeout: float = 3.0) -> bool:
    """Snelle probe: is RFCOMM SPP4 bereikbaar ook als PnP 'disconnected' zegt?

    Serialiseert via hetzelfde lock om 10048 te voorkomen; korte timeout.
    """
    acquired = _rfcomm_lock.acquire(timeout=8)
    if not acquired:
        return False
    try:
        ch = resolve_channel(address, channel)
        try:
            transport.bluetooth_family()
        except transport.Unsupported:
            return False
        sess = transport.Session(address, ch, _null_log,
                                 attempts=attempts, retry_delay=retry_delay,
                                 connect_timeout=timeout)
        try:
            sess.open()
            return True
        except Exception:
            return False
        finally:
            sess.close()
    finally:
        _rfcomm_lock.release()


# ---------------------------------------------------------------------------
# Background monitor — pollt pairing/connection en auto-applied fix
# ---------------------------------------------------------------------------

class BudsMonitor(threading.Thread):
    """Pollt elke poll_interval; bij nieuwe connectie -> apply_fix.

    Robuust tegen PnP mismatch (extra RFCOMM retry) en power/case-cycle
    (asVer terug op 1) via periodieke re-verify en startup-check.
    """

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
        self._last_verify_ts: float = 0.0
        self._last_disconnected_probe: dict[str, float] = {}
        self._last_status: BudsStatus | None = None
        self._startup_done = False
        self._pnp_stable: dict[str, tuple[bool, int]] = {}  # addr -> (last_raw, stable_count)

    def stop(self):
        self._stop.set()

    def _stable_is_now(self, addr: str, raw_connected: bool) -> bool:
        """PnP flapt soms per poll tussen OK/Unknown. Eis 2x zelfde waarde voor transitie."""
        last_raw, cnt = self._pnp_stable.get(addr, (raw_connected, 0))
        if raw_connected == last_raw:
            cnt = min(cnt + 1, 10)
        else:
            cnt = 1
            last_raw = raw_connected
        self._pnp_stable[addr] = (last_raw, cnt)
        # Bij eerste waarneming meteen stabiel, daarna pas na 2 hits
        if cnt >= 2:
            return raw_connected
        # tijdens instabiele fase: behoud vorige stabiele is_now
        prev = self._last_connected.get(addr)
        if prev is None:
            return raw_connected  # eerste keer geen historie
        return prev

    def _maybe_fix(self, addr: str, name: str, channel: int | None,
                   as_ver: int, attempts: int, retry_delay: float,
                   listen_seconds: float, reason: str) -> None:
        now = time.monotonic()
        debounce = float(self.config.get("debounce_seconds", 20.0))
        # Re-apply na case/power-cycle (asVer terug op 1) moet debounce bypassen,
        # anders blijft multipoint geblokkeerd tot debounce verloopt
        bypass = reason in ("startup",) or reason.startswith("asVer=")
        if now - self._last_fix_ts < debounce and not bypass:
            log.info("fix skip %s debounce %.0fs rest (%s)", addr,
                     debounce - (now - self._last_fix_ts), reason)
            return
        log.info("auto fix trigger %s \"%s\" reason=%s asVer=%d", addr, name, reason, as_ver)
        if self.on_event:
            self.on_event(f"Buds {name} — fix wordt toegepast ({reason})…")
        # SPP server komt ~0.5-1s na ACL up
        time.sleep(0.8)
        ok, msg = apply_fix(addr, channel, as_ver=as_ver,
                            attempts=max(attempts, 10),
                            retry_delay=retry_delay,
                            listen_seconds=listen_seconds)
        self._last_fix_ts = time.monotonic()
        self._last_verify_ts = self._last_fix_ts
        level = logging.INFO if ok else logging.WARNING
        log.log(level, "auto fix %s (%s): %s", addr, reason, msg)
        if self.on_event:
            self.on_event(f"Fix {'ok' if ok else 'mislukt'}: {msg}")

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
        verify_interval = float(self.config.get("verify_interval", 90.0))
        disconnected_retry = float(self.config.get("disconnected_retry_seconds", 25.0))

        log.info("monitor start: needle=%r poll=%.1fs debounce=%.1fs verify=%.0fs auto_apply=%s",
                 name_needle, poll, debounce, verify_interval, auto_apply)

        # Direct bij opstart: status-check en indien gekoppeld meteen fix proberen
        # (dekt cold-boot, login terwijl Buds al verbonden zijn, en case-cycle tijdens uit).
        # Alleen fixen bij harde mismatch; geen-state is geen bewijs dat fix nodig is.
        if auto_apply:
            try:
                if address:
                    dev0 = find_buds(name_needle, address)
                    cands0 = [dev0] if dev0 else []
                else:
                    cands0 = paired_buds(name_needle)
                for dev in cands0:
                    # Alleen als PnP zegt verbonden, of heel kort RFCOMM probe lukt
                    reachable = dev.connected
                    if not reachable:
                        # korte check zonder te spammen — alleen bij startup
                        reachable = _can_open_rfcomm(dev.address, channel, attempts=2, retry_delay=0.3, timeout=2.5)
                    if reachable:
                        _, _, reported = _do_rfcomm_write(
                            dev.address, resolve_channel(dev.address, channel),
                            as_ver, attempts=max(attempts, 6),
                            retry_delay=retry_delay, listen_seconds=listen_seconds)
                        if reported is not None and reported != as_ver:
                            log.info("startup-check %s \"%s\" asVer=%s -> fix", dev.address, dev.name, reported)
                            self._maybe_fix(dev.address, dev.name, channel, as_ver, attempts, retry_delay, listen_seconds, reason="startup")
                        elif reported is not None:
                            log.info("startup-check %s asVer=%s ok — geen fix nodig", dev.address, reported)
                            self._last_fix_ts = time.monotonic()
                            self._last_verify_ts = time.monotonic()
                        else:
                            log.info("startup-check %s geen state frame — geen conclusie, geen auto-fix", dev.address)
                            # geen timestamp update, zodat connect-event later alsnog kan fixen
                            self._last_verify_ts = time.monotonic()
                self._startup_done = True
            except Exception as exc:
                log.debug("startup check faalde (niet fataal): %s", exc)
                self._startup_done = True

        while not self._stop.is_set():
            try:
                if address:
                    dev = find_buds(name_needle, address)
                    candidates = [dev] if dev else []
                else:
                    candidates = paired_buds(name_needle)

                if not candidates:
                    if self._last_status is None or self._last_status.paired:
                        st = BudsStatus(False, False, None, None, None, None, None)
                        self._last_status = st
                        if self.on_status:
                            self.on_status(st)
                    self._last_connected.clear()
                    self._last_disconnected_probe.clear()
                    self._stop.wait(poll)
                    continue

                now = time.monotonic()
                for dev in candidates:
                    addr = dev.address
                    was = self._last_connected.get(addr)
                    pnp_raw = bool(dev.connected)
                    # Stabiliseer PnP: eis 2 polls zelfde waarde voor transitie
                    pnp_connected = self._stable_is_now(addr, pnp_raw)
                    # Ground truth: als gestabiliseerd PnP nee zegt maar RFCOMM wel open kan, toch connected
                    is_now = pnp_connected
                    probed = False
                    if not pnp_connected and auto_apply:
                        last_probe = self._last_disconnected_probe.get(addr, 0)
                        if now - last_probe >= disconnected_retry:
                            self._last_disconnected_probe[addr] = now
                            if _can_open_rfcomm(addr, channel, attempts=2, retry_delay=0.4, timeout=2.5):
                                log.info("PnP zegt disconnected maar RFCOMM open lukt %s \"%s\" — behandel als connected", addr, dev.name)
                                is_now = True
                                probed = True
                                if self.on_event:
                                    self.on_event(f"Buds {dev.name}: PnP mismatch — toch verbonden, fix check…")

                    if dev == candidates[0]:
                        st = BudsStatus(True, is_now, dev.address, dev.name, None, None, None)
                        self._last_status = st
                        if self.on_status:
                            self.on_status(st)

                    if was is True and is_now is False and not probed:
                        log.info("disconnect gedetecteerd %s \"%s\"", addr, dev.name)
                        self._last_connected[addr] = False
                        # reset verify zodat volgende connect snel re-verifieert
                        self._last_verify_ts = 0
                        if self.on_event:
                            self.on_event(f"Buds disconnected: {dev.name}")
                        continue

                    if (was is False and is_now is True) or (was is None and is_now is True):
                        self._maybe_fix(addr, dev.name, channel, as_ver, attempts, retry_delay, listen_seconds, reason="connect")
                        self._last_connected[addr] = True
                        continue

                    # Al verbonden — periodieke re-verify voor case/power-cycle
                    # Als asVer terug op 1 staat (na case), herhaal fix zonder user-interactie
                    if is_now and auto_apply and self._startup_done:
                        if now - self._last_verify_ts >= verify_interval:
                            self._last_verify_ts = now
                            try:
                                ch = resolve_channel(addr, channel)
                                _, msg, reported = _do_rfcomm_write(addr, ch, as_ver,
                                                                     attempts=max(attempts, 6),
                                                                     retry_delay=retry_delay,
                                                                     listen_seconds=listen_seconds)
                                if reported is not None and reported != as_ver:
                                    log.warning("re-verify %s asVer=%s verwacht %s -> re-apply (case/power-cycle?)", addr, reported, as_ver)
                                    self._maybe_fix(addr, dev.name, channel, as_ver, attempts, retry_delay, listen_seconds, reason=f"asVer={reported}->re-apply")
                                elif reported is None:
                                    # Geen state maar wel verzonden — niet als fout tellen, wel log
                                    log.debug("re-verify %s geen state frame: %s", addr, msg)
                                else:
                                    log.debug("re-verify %s asVer=%s ok", addr, reported)
                            except Exception as exc:
                                log.debug("re-verify %s faalde: %s", addr, exc)

                    self._last_connected[addr] = is_now

            except Exception as exc:
                log.exception("monitor loop error: %s", exc)
                if self.on_event:
                    self.on_event(f"Monitor fout: {exc} (blijft actief)")

            self._stop.wait(poll)

        log.info("monitor gestopt")
