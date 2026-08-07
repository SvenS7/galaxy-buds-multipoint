"""RFCOMM client transport for Linux and Windows.

macOS is deliberately absent: IOBluetooth has no usable Python binding, and its
privacy prompt needs a signed .app bundle, so the macOS tool is a separate Swift
program in ../../macos.

CPython exposes Bluetooth sockets almost identically on both platforms handled
here — `AF_BLUETOOTH` + `BTPROTO_RFCOMM`, addressed as `(address, channel)`. On
Windows those two names are compile-time aliases for `AF_BTH`/`BTHPROTO_RFCOMM`,
but not every build exports both spellings, so both are looked up.

Standard library only.
"""

from __future__ import annotations

import select
import socket
import sys
import time

DEFAULT_ATTEMPTS = 8
DEFAULT_RETRY_DELAY = 0.7
DEFAULT_CONNECT_TIMEOUT = 10.0
SEND_GAP = 0.4


class Unsupported(RuntimeError):
    """This platform (or this Python build) cannot open an RFCOMM socket."""


class OpenFailed(RuntimeError):
    """The channel could not be opened within the allowed attempts."""


def _lookup(*names: str) -> int | None:
    for n in names:
        v = getattr(socket, n, None)
        if v is not None:
            return int(v)
    return None


def bluetooth_family() -> int:
    """The socket family for Bluetooth on this platform."""
    if sys.platform == "darwin":
        raise Unsupported(
            "macOS is not supported here — use the Swift tool in macos/ "
            "(IOBluetooth needs an app bundle for the Bluetooth privacy prompt)"
        )
    family = _lookup("AF_BLUETOOTH", "AF_BTH")
    if family is None:
        raise Unsupported(
            f"this Python has no Bluetooth socket support on {sys.platform} "
            "(socket.AF_BLUETOOTH is missing)"
        )
    return family


def rfcomm_proto() -> int:
    proto = _lookup("BTPROTO_RFCOMM", "BTHPROTO_RFCOMM")
    if proto is None:
        raise Unsupported("this Python has no socket.BTPROTO_RFCOMM")
    return proto


def errstr(exc: OSError) -> str:
    """Render an OSError the way the platform names it, number included."""
    code = getattr(exc, "winerror", None) or exc.errno
    detail = exc.strerror or str(exc)
    return f"{detail} ({code})" if code else detail


class Session:
    """One RFCOMM conversation: open with retries, write frames, then listen."""

    def __init__(self, address: str, channel: int, log,
                 attempts: int = DEFAULT_ATTEMPTS,
                 retry_delay: float = DEFAULT_RETRY_DELAY,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT):
        self.address = address
        self.channel = channel
        self.log = log
        self.attempts = max(1, attempts)
        self.retry_delay = retry_delay
        self.connect_timeout = connect_timeout
        self.sock: socket.socket | None = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Connect, retrying — the buds' SPP server takes a moment to come up."""
        family, proto = bluetooth_family(), rfcomm_proto()
        last = ""
        for attempt in range(1, self.attempts + 1):
            s = socket.socket(family, socket.SOCK_STREAM, proto)
            s.settimeout(self.connect_timeout)
            try:
                s.connect((self.address, self.channel))
            except OSError as exc:
                s.close()
                last = errstr(exc)
                self.log(f"open ch{self.channel} attempt {attempt}/{self.attempts} => {last}")
                if attempt < self.attempts:
                    time.sleep(self.retry_delay)
                continue
            self.log(f"open ch{self.channel} attempt {attempt}/{self.attempts} => OPEN")
            self.sock = s
            return
        raise OpenFailed(f"rfcomm open failed after {self.attempts} attempts: {last}")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self) -> "Session":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- traffic -----------------------------------------------------------

    def send(self, frames: list[bytes], gap: float = SEND_GAP) -> None:
        assert self.sock is not None, "open() first"
        for i, f in enumerate(frames):
            self.sock.sendall(f)
            self.log(f"TX[{i}] {len(f)}B {f.hex()}")
            if i + 1 < len(frames):
                time.sleep(gap)

    def listen(self, seconds: float) -> bytes:
        """Collect whatever the buds push for `seconds`, then give up politely."""
        assert self.sock is not None, "open() first"
        if seconds <= 0:
            return b""
        rx = bytearray()
        deadline = time.monotonic() + seconds
        self.sock.setblocking(False)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([self.sock], [], [], min(remaining, 0.5))
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = self.sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                self.log(f"channel error while reading: {errstr(exc)}")
                break
            if not chunk:
                self.log("channel closed by peer")
                break
            rx += chunk
            self.log(f"RX {len(chunk)}B {chunk.hex()}")
        self.log(f"total RX {len(rx)}B")
        return bytes(rx)
