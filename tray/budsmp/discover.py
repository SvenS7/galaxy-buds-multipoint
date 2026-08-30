"""Finding the buds and their RFCOMM channel, without any dependencies.

Neither platform exposes paired devices or cached SDP records to a plain socket
API, so this shells out to the tools that ship with the OS: `bluetoothctl` on
Linux and `Get-PnpDevice` on Windows. Everything here degrades to "pass --addr
and --channel yourself" rather than failing hard, which is why each helper
returns best-effort results instead of raising.

The macOS tool gets both for free from IOBluetooth; see macos/Sources/main.swift.
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import sys
from typing import Iterable, NamedTuple

# SPPSERVICE4 as advertised by Galaxy Buds — the channel MDE_VERSION rides on.
SPP4_SERVICE_NAME = "SPPSERVICE4"
SPP4_FALLBACK_CHANNEL = 29

_ADDR_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_BLUETOOTHCTL_RE = re.compile(r"^Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+(.*)$")
_PNP_ADDR_RE = re.compile(r"DEV_([0-9A-Fa-f]{12})")


class Device(NamedTuple):
    address: str
    name: str
    connected: bool


def is_address(s: str) -> bool:
    return bool(_ADDR_RE.match(s))


def normalize_address(s: str) -> str:
    """Accept AA:BB:.., aabbcc.., aa-bb-.. — emit the colon-separated upper form."""
    raw = re.sub(r"[^0-9A-Fa-f]", "", s)
    if len(raw) != 12:
        raise ValueError(f"not a Bluetooth address: {s!r}")
    raw = raw.upper()
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


def _hidden_kwargs() -> dict:
    """Hide console window on Windows — prevents flash every 3s during polling."""
    if sys.platform == "win32":
        try:
            si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
            si.wShowWindow = 0  # SW_HIDE — required with STARTF_USESHOWWINDOW
            return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
        except Exception:
            pass
    return {}


def _run(cmd: list[str], timeout: float = 12.0) -> str | None:
    """Run a helper and return stdout, or None if it is missing or unhappy."""
    if not shutil.which(cmd[0]):
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **_hidden_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


# ---------------------------------------------------------------------------
# Linux — bluetoothctl / sdptool
# ---------------------------------------------------------------------------

def _linux_bluetoothctl(*args: str) -> str | None:
    return _run(["bluetoothctl", *args])


def _linux_device_lines(out: str) -> Iterable[tuple[str, str]]:
    for line in out.splitlines():
        m = _BLUETOOTHCTL_RE.match(line.strip())
        if m:
            yield normalize_address(m.group(1)), m.group(2).strip()


def _linux_paired() -> list[Device]:
    # `devices Paired` is BlueZ >= 5.65; `paired-devices` is the older spelling.
    out = _linux_bluetoothctl("devices", "Paired") or _linux_bluetoothctl("paired-devices")
    if out is None:
        # Last resort: every known device, paired or not.
        out = _linux_bluetoothctl("devices") or ""
    connected = {a for a, _ in _linux_device_lines(_linux_bluetoothctl("devices", "Connected") or "")}
    return [Device(addr, name, addr in connected) for addr, name in _linux_device_lines(out)]


def _linux_spp4_channel(address: str) -> int | None:
    """Parse `sdptool browse` for SPPSERVICE4's RFCOMM channel.

    sdptool lives in the deprecated BlueZ tools and is missing on many distros,
    hence the None return rather than an error.
    """
    out = _run(["sdptool", "browse", address], timeout=20.0)
    if not out:
        return None
    name, channel = None, None
    for line in out.splitlines() + [""]:
        stripped = line.strip()
        if not stripped:                                  # records are blank-line separated
            if name == SPP4_SERVICE_NAME and channel is not None:
                return channel
            name, channel = None, None
            continue
        if stripped.startswith("Service Name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Channel:"):
            try:
                channel = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                channel = None
    return None


# ---------------------------------------------------------------------------
# Windows — Get-PnpDevice
# ---------------------------------------------------------------------------

_PNP_QUERY = (
    "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue "
    "| Select-Object FriendlyName,InstanceId,Status | ConvertTo-Csv -NoTypeInformation"
)


def _windows_paired() -> list[Device]:
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PNP_QUERY], timeout=30.0)
    if out is None:
        out = _run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", _PNP_QUERY], timeout=30.0)
    if not out:
        return []
    devices: dict[str, Device] = {}
    for row in csv.DictReader(io.StringIO(out)):
        m = _PNP_ADDR_RE.search(row.get("InstanceId") or "")
        if not m:                                        # the radio itself, not a peer
            continue
        addr = normalize_address(m.group(1))
        name = (row.get("FriendlyName") or "").strip() or "(unnamed)"
        # A peer's device node reports OK while it is connected and Unknown once
        # it goes out of range, so this is a proxy for "connected", not a fact.
        connected = (row.get("Status") or "").strip().upper() == "OK"
        prev = devices.get(addr)
        # One address yields several nodes (one per service); keep the richest.
        if prev is None or (connected and not prev.connected) or len(name) > len(prev.name):
            devices[addr] = Device(addr, name, connected or (prev.connected if prev else False))
    return sorted(devices.values(), key=lambda d: d.name.lower())


# ---------------------------------------------------------------------------
# Platform-agnostic entry points
# ---------------------------------------------------------------------------

def paired_devices() -> list[Device]:
    if sys.platform.startswith("linux"):
        return _linux_paired()
    if sys.platform == "win32":
        return _windows_paired()
    return []


def find_device(address: str | None, name_needle: str, log) -> Device | None:
    """Resolve --addr, or the first paired device whose name contains the needle."""
    if address is not None:
        try:
            addr = normalize_address(address)
        except ValueError as exc:
            log(str(exc))
            return None
        known = {d.address: d for d in paired_devices()}
        return known.get(addr, Device(addr, "(from --addr)", False))

    needle = name_needle.lower()
    devices = paired_devices()
    if not devices:
        log("could not enumerate paired devices on this system")
        log("pass the address yourself: budsmp apply --addr XX:XX:XX:XX:XX:XX")
        return None
    matches = [d for d in devices if needle in d.name.lower()]
    if not matches:
        log(f'no paired device whose name contains "{name_needle}"')
        log("run `budsmp scan` to list paired devices, then pass --addr <XX:XX:XX:XX:XX:XX>")
        return None
    if len(matches) > 1:
        log(f'note: {len(matches)} paired devices match "{name_needle}"; preferring a connected one')
    for d in matches:
        if d.connected:
            return d
    return matches[0]


def resolve_channel(address: str, override: int | None, log) -> int:
    if override is not None:
        log(f"using RFCOMM channel {override} (from --channel)")
        return override
    if sys.platform.startswith("linux"):
        ch = _linux_spp4_channel(address)
        if ch is not None:
            log(f"resolved {SPP4_SERVICE_NAME} -> RFCOMM channel {ch} from SDP")
            return ch
        log("could not read SDP records (sdptool missing or the query failed)")
    else:
        log("no SDP query available on this platform")
    log(f"falling back to channel {SPP4_FALLBACK_CHANNEL}, which is where Galaxy Buds "
        f"put {SPP4_SERVICE_NAME}")
    log("  (override with --channel if your model differs)")
    return SPP4_FALLBACK_CHANNEL


def describe_services(address: str, log) -> int:
    """`budsmp sdp`: dump the RFCOMM channel map. Linux only."""
    if not sys.platform.startswith("linux"):
        log("an SDP dump needs `sdptool`, which only exists on Linux")
        log("on Windows, pass --channel explicitly if 29 is wrong for your model")
        return 0
    out = _run(["sdptool", "browse", address], timeout=20.0)
    if not out:
        log("sdptool is not installed or the query failed")
        log("  Debian/Ubuntu: apt install bluez-tools  (sdptool ships in bluez on older releases)")
        return 0
    shown = 0
    name = channel = None
    for line in out.splitlines() + [""]:
        stripped = line.strip()
        if not stripped:
            if name is not None:
                mark = "   <-- MDE_VERSION channel" if name == SPP4_SERVICE_NAME else ""
                log(f"  rfcomm {channel if channel is not None else ' -':>3}  {name}{mark}")
                shown += 1
            name = channel = None
            continue
        if stripped.startswith("Service Name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Channel:"):
            try:
                channel = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                channel = None
    return shown
