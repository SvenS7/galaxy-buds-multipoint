"""Buds detection, connect detection and fix logic.

Reuses existing budsmp primitives (frame/transport/discover) without forking.
Fully local, stdlib + budsmp.
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

# Global lock: Buds have only one RFCOMM SPP4 socket; concurrent opens
# from monitor + UI (StatusWindow) cause WSAEADDRINUSE 10048. This lock serializes all opens.
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
    """Find target device (same resolution as cli.py)."""
    dev = discover.find_device(address, name_needle, _null_log)
    return dev


def paired_buds(name_needle: str = "buds") -> list:
    """All paired devices whose name contains the needle."""
    all_devs = discover.paired_devices()
    needle = name_needle.lower()
    return [d for d in all_devs if needle in d.name.lower()]


def resolve_channel(address: str, override: int | None) -> int:
    return discover.resolve_channel(address, override, _null_log)


def _do_rfcomm_write(address: str, channel: int, as_ver: int,
                     attempts: int, retry_delay: float,
                     listen_seconds: float) -> tuple[bool, str, int | None]:
    """Perform one RFCOMM write. Returns (ok, msg, reported_as_ver). Serialized via _rfcomm_lock."""
    acquired = _rfcomm_lock.acquire(timeout=30)
    if not acquired:
        return False, "RFCOMM busy (other operation active)", None
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
            return True, "sent (no state frame — start/stop playback and check again)", None

        reported = states[-1].as_ver
        allowed = frame.multipoint_allowed(reported)
        if reported == as_ver or (as_ver == 0 and reported <= 1):
            return True, f"asVer={reported} ({'multipoint allowed' if allowed else 'blocked'})", reported
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
    # mismatch is "not verified" but still sent; only open failure is a hard error
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
    """Report pairing, connection and presumed fix status.

    Does a read-like probe: writes as_ver_probe again (no-op) to trigger
    a state NOTIFY, like cli `read` does.
    """
    dev = find_buds(name_needle, address)
    if dev is None:
        all_devs = discover.paired_devices()
        if not all_devs:
            return BudsStatus(False, False, None, None, None, None, "no paired devices found")
        return BudsStatus(False, False, None, None, None, None,
                          f'no paired device matching "{name_needle}" (scan shows {len(all_devs)} device(s))')

    ch = resolve_channel(dev.address, channel)
    ok, msg, reported = _do_rfcomm_write(dev.address, ch, as_ver_probe,
                                         attempts=attempts, retry_delay=retry_delay,
                                         listen_seconds=listen_seconds)
    if reported is not None:
        return BudsStatus(True, dev.connected, dev.address, dev.name,
                          reported, frame.multipoint_allowed(reported), None)
    if not ok and "open failed" in msg.lower():
        return BudsStatus(True, dev.connected, dev.address, dev.name, None, None, msg)
    return BudsStatus(True, dev.connected, dev.address, dev.name, None, None,
                      "no state frame (buds sleeping? start playback)")


# ---------------------------------------------------------------------------
# Helpers voor robuuste detectie
# ---------------------------------------------------------------------------

def _can_open_rfcomm(address: str, channel: int | None, attempts: int = 2,
                     retry_delay: float = 0.4, timeout: float = 3.0) -> bool:
    """Quick probe: is RFCOMM SPP4 reachable even if PnP says 'disconnected'?

    Serialized via same lock to avoid 10048; short timeout.
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
    """Polls every poll_interval; on new connection -> apply_fix.

    Robust against PnP mismatch (extra RFCOMM retry) and power/case-cycle
    (asVer back to 1) via periodic re-verify and startup check.
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
        """PnP sometimes flaps per poll between OK/Unknown. Require 2x same value."""
        last_raw, cnt = self._pnp_stable.get(addr, (raw_connected, 0))
        if raw_connected == last_raw:
            cnt = min(cnt + 1, 10)
        else:
            cnt = 1
            last_raw = raw_connected
        self._pnp_stable[addr] = (last_raw, cnt)
        if cnt >= 2:
            return raw_connected
        prev = self._last_connected.get(addr)
        if prev is None:
            return raw_connected
        return prev

    def _maybe_fix(self, addr: str, name: str, channel: int | None,
                   as_ver: int, attempts: int, retry_delay: float,
                   listen_seconds: float, reason: str) -> None:
        now = time.monotonic()
        debounce = float(self.config.get("debounce_seconds", 20.0))
        # Re-apply after case/power-cycle (asVer back to 1) must bypass debounce
        bypass = reason in ("startup",) or reason.startswith("asVer=")
        if now - self._last_fix_ts < debounce and not bypass:
            log.info("fix skip %s debounce %.0fs remaining (%s)", addr,
                     debounce - (now - self._last_fix_ts), reason)
            return
        log.info("auto fix trigger %s \"%s\" reason=%s asVer=%d", addr, name, reason, as_ver)
        if self.on_event:
            self.on_event(f"Buds {name} — applying fix ({reason})…")
        # SPP server comes up ~0.5-1s after ACL up
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
            self.on_event(f"Fix {'ok' if ok else 'failed'}: {msg}")

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

        # Immediate at startup: status check and auto-fix if paired/connected
        # (covers cold boot, login while Buds already connected, case-cycle while off).
        if auto_apply:
            try:
                if address:
                    dev0 = find_buds(name_needle, address)
                    cands0 = [dev0] if dev0 else []
                else:
                    cands0 = paired_buds(name_needle)
                for dev in cands0:
                    reachable = dev.connected
                    if not reachable:
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
                            log.info("startup-check %s asVer=%s ok — no fix needed", dev.address, reported)
                            self._last_fix_ts = time.monotonic()
                            self._last_verify_ts = time.monotonic()
                        else:
                            log.info("startup-check %s no state frame — inconclusive, no auto-fix", dev.address)
                            self._last_verify_ts = time.monotonic()
                self._startup_done = True
            except Exception as exc:
                log.debug("startup check failed (non-fatal): %s", exc)
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
                    pnp_connected = self._stable_is_now(addr, pnp_raw)
                    is_now = pnp_connected
                    probed = False
                    if not pnp_connected and auto_apply:
                        last_probe = self._last_disconnected_probe.get(addr, 0)
                        if now - last_probe >= disconnected_retry:
                            self._last_disconnected_probe[addr] = now
                            if _can_open_rfcomm(addr, channel, attempts=2, retry_delay=0.4, timeout=2.5):
                                log.info("PnP says disconnected but RFCOMM open succeeded %s \"%s\" — treat as connected", addr, dev.name)
                                is_now = True
                                probed = True
                                if self.on_event:
                                    self.on_event(f"Buds {dev.name}: PnP mismatch — still connected, checking fix…")

                    if dev == candidates[0]:
                        st = BudsStatus(True, is_now, dev.address, dev.name, None, None, None)
                        self._last_status = st
                        if self.on_status:
                            self.on_status(st)

                    if was is True and is_now is False and not probed:
                        log.info("disconnect detected %s \"%s\"", addr, dev.name)
                        self._last_connected[addr] = False
                        self._last_verify_ts = 0
                        if self.on_event:
                            self.on_event(f"Buds disconnected: {dev.name}")
                        continue

                    if (was is False and is_now is True) or (was is None and is_now is True):
                        self._maybe_fix(addr, dev.name, channel, as_ver, attempts, retry_delay, listen_seconds, reason="connect")
                        self._last_connected[addr] = True
                        continue

                    # Already connected — periodic re-verify for case/power-cycle
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
                                    log.warning("re-verify %s asVer=%s expected %s -> re-apply (case/power-cycle?)", addr, reported, as_ver)
                                    self._maybe_fix(addr, dev.name, channel, as_ver, attempts, retry_delay, listen_seconds, reason=f"asVer={reported}->re-apply")
                                elif reported is None:
                                    log.debug("re-verify %s no state frame: %s", addr, msg)
                                else:
                                    log.debug("re-verify %s asVer=%s ok", addr, reported)
                            except Exception as exc:
                                log.debug("re-verify %s failed: %s", addr, exc)

                    self._last_connected[addr] = is_now

            except Exception as exc:
                log.exception("monitor loop error: %s", exc)
                if self.on_event:
                    self.on_event(f"Monitor error: {exc} (stays active)")

            self._stop.wait(poll)

        log.info("monitor stopped")
