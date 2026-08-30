"""Samsung SMEP frame construction and decoding.

Framing, MDE_VERSION layout and the device-state NOTIFY structure are all
documented in ../../docs/protocol.md. This module is the Python source of truth
for them; macos/Sources/main.swift carries an independent implementation, and the
two are checked against each other by producing identical bytes for the frames
captured from real hardware (see the self-test at the bottom of this file).

Standard library only, deliberately: this has to run on a stock Python on Linux
and Windows with nothing installed.

Run it directly to build or decode frames:

    python3 -m budsmp.frame version-only 2
    python3 -m budsmp.frame account 2 01 aabb
    python3 -m budsmp.frame decode fc0b00014304030400000b021eafcc
    python3 -m budsmp.frame selftest
"""

from __future__ import annotations

import sys
from typing import Iterator, NamedTuple

SOM = 0xFC
EOM = 0xCC

MSG_SET = 0x43     # write attribute
MSG_GET = 0x44     # read attribute
MSG_NOTIFY = 0x45  # pushed by the buds

MDE_VERSION = 0x0B

# Payload prefix of the device-state NOTIFY that reports the stored record.
STATE_PREFIX = bytes([0x02, 0x05, 0x4C, MDE_VERSION])
# Literal that immediately precedes the peer's account hash inside that payload.
PEER_ACCOUNT_ANCHOR = bytes([0xEB, 0x1A, 0x00])

# The complete fix: MDE_VERSION SET, version-only, asVer=2.
FIX_FRAME_HEX = "fc0b00014304030400000b021eafcc"


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------

def crc16_xmodem(data: bytes) -> int:
    """CRC16-CCITT/XMODEM: poly 0x1021, init 0x0000, no reflection, no final xor."""
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def build(msg_id: int, payload: bytes, seq: int = 0) -> bytes:
    """Wrap a payload in SMEP framing.

    The length field counts from byte 3 through the end of the CRC, and the CRC
    covers that same region minus the CRC itself. The sequence bits sit in
    byte 2, outside the CRC range, so changing seq leaves the checksum alone.
    """
    plen = 1 + 1 + len(payload) + 2
    if plen > 0x3FF:
        raise ValueError(f"payload too long for a single frame: {len(payload)} bytes")
    i2 = (plen & 0x3FF) | ((seq & 3) << 14)
    core = bytes([0x01, msg_id]) + payload
    crc = crc16_xmodem(core)
    return (bytes([SOM, i2 & 0xFF, (i2 >> 8) & 0xFF])
            + core
            + bytes([crc & 0xFF, (crc >> 8) & 0xFF, EOM]))


def _mde_set(blob: bytes, seq: int = 0) -> bytes:
    """SET carrying a single TLV: tag 0x04, type 0x03 (byte blob)."""
    return build(MSG_SET, bytes([0x04, 0x03, len(blob)]) + blob, seq)


def version_only(version: int, seq: int = 0) -> bytes:
    """MDE_VERSION with just the version byte; leaves the account field alone.

    This is the frame the fix needs. `version_only(2)` is FIX_FRAME_HEX.
    """
    if not 0 <= version <= 3:
        raise ValueError("the firmware only accepts version <= 3")
    return _mde_set(bytes([0x00, 0x00, MDE_VERSION, version]), seq)


def with_account(version: int, selector: int, account: int, seq: int = 0) -> bytes:
    """MDE_VERSION that also overwrites the stored 2-byte account hash.

    Not needed for multipoint — see ../../docs/experiments.md. Kept because it is
    the form a Galaxy phone actually sends, and because overwriting the account is
    how the experiment that proved it unnecessary was performed.
    """
    if not 0 <= version <= 3:
        raise ValueError("the firmware only accepts version <= 3")
    if not 0 <= account <= 0xFFFF:
        raise ValueError("the account hash is 2 bytes")
    return _mde_set(bytes([0x00, 0x00, MDE_VERSION, version, selector,
                           (account >> 8) & 0xFF, account & 0xFF]), seq)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class Frame(NamedTuple):
    msg_id: int
    payload: bytes
    seq: int
    crc_ok: bool
    raw: bytes


def parse(stream: bytes) -> Iterator[Frame]:
    """Yield SMEP frames from a raw RFCOMM byte stream.

    Resynchronises on SOM, so a stream that starts mid-frame or contains junk
    between frames still yields the frames it does contain.
    """
    i = 0
    n = len(stream)
    while i < n:
        if stream[i] != SOM:
            i += 1
            continue
        if i + 4 > n:
            return
        i2 = (stream[i + 2] << 8) | stream[i + 1]
        total = (i2 & 0x3FF) + 4
        if total < 8 or i + total > n or stream[i + total - 1] != EOM:
            i += 1
            continue
        raw = stream[i:i + total]
        core = raw[3:-3]
        want = raw[-3] | (raw[-2] << 8)
        yield Frame(msg_id=raw[4],
                    payload=raw[5:-3],
                    seq=(i2 >> 14) & 3,
                    crc_ok=crc16_xmodem(core) == want,
                    raw=raw)
        i += total


class DeviceState(NamedTuple):
    as_ver: int
    declared_account: int
    peer_accounts: tuple[int, ...]


def decode_state(stream: bytes) -> list[DeviceState]:
    """Pull every device-state report out of a stream of NOTIFY frames.

    Layout, from ../../docs/protocol.md:
      offset 6    asVer stored for the host that is talking
      offset 8-9  account that host declared, little-endian
      after the 'eb 1a 00' anchor: the other peer's account, little-endian
    """
    out: list[DeviceState] = []
    for f in parse(stream):
        p = f.payload
        if len(p) < 10 or not p.startswith(STATE_PREFIX):
            continue
        peers = []
        start = 0
        while True:
            k = p.find(PEER_ACCOUNT_ANCHOR, start)
            if k < 0 or k + 5 > len(p):
                break
            peers.append(int.from_bytes(p[k + 3:k + 5], "little"))
            start = k + 1
        out.append(DeviceState(as_ver=p[6],
                               declared_account=int.from_bytes(p[8:10], "little"),
                               peer_accounts=tuple(peers)))
    return out


def multipoint_allowed(as_ver: int) -> bool:
    """The whole gate, in one line."""
    return as_ver in (2, 3)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def selftest() -> int:
    """Check the builder against bytes captured from real hardware."""
    checks = [
        ("version-only asVer=2 (the fix)", version_only(2), FIX_FRAME_HEX),
        ("version-only asVer=0 (revert)", version_only(0),
         "fc0b00014304030400000b005c8fcc"),
        ("account form, asVer=2 hash=dead", with_account(2, 0x01, 0xDEAD),
         "fc0e00014304030700000b0201deadfb1ccc"),
        ("account form, zeroed account", with_account(2, 0x00, 0x0000),
         "fc0e00014304030700000b02000000a479cc"),
    ]
    failed = 0
    for label, got, want in checks:
        ok = got.hex() == want
        failed += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] {label}\n         {got.hex()}"
              + ("" if ok else f"\n         expected {want}"))

    # Round-trip: everything we build must parse back with a valid CRC.
    for label, got, _ in checks:
        frames = list(parse(got))
        ok = len(frames) == 1 and frames[0].crc_ok and frames[0].msg_id == MSG_SET
        failed += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] round-trip: {label}")

    # A frame embedded in noise must still be found.
    noisy = bytes([0x00, 0xFC, 0x99]) + version_only(2) + bytes([0xFF])
    found = [f.raw.hex() for f in parse(noisy)]
    ok = found == [FIX_FRAME_HEX]
    failed += not ok
    print(f"  [{'ok' if ok else 'FAIL'}] resynchronises past junk bytes")

    print("all checks passed" if not failed else f"{failed} check(s) FAILED")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """usage: python3 -m budsmp.frame <mode> [args]

  version-only <ver>                  the fix: MDE_VERSION with no account bytes
  account <ver> <selector> <hhhh>     MDE_VERSION that also writes an account hash
  decode <hex>                        take a frame or stream apart
  selftest                            check the builder against captured bytes

examples:
  python3 -m budsmp.frame version-only 2
  python3 -m budsmp.frame account 2 01 aabb
  python3 -m budsmp.frame decode fc0b00014304030400000b021eafcc
"""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    mode = argv[0]
    try:
        if mode == "version-only":
            ver = int(argv[1], 0)
            print(version_only(ver).hex())
            return 0
        if mode == "account":
            ver, sel, acct = int(argv[1], 0), int(argv[2], 16), int(argv[3], 16)
            print(with_account(ver, sel, acct).hex())
            return 0
        if mode == "decode":
            stream = bytes.fromhex(argv[1].replace(" ", "").replace(":", ""))
            frames = list(parse(stream))
            if not frames:
                print("no SMEP frame found")
                return 1
            for f in frames:
                print(f"msgID=0x{f.msg_id:02x} seq={f.seq} "
                      f"crc={'ok' if f.crc_ok else 'BAD'} "
                      f"payload={f.payload.hex()}")
            for st in decode_state(stream):
                peers = ", ".join(f"0x{a:04x}" for a in st.peer_accounts) or "-"
                print(f"  device state: asVer={st.as_ver} "
                      f"({'multipoint allowed' if multipoint_allowed(st.as_ver) else 'multipoint blocked'})"
                      f" declared=0x{st.declared_account:04x} peers={peers}")
            return 0
        if mode == "selftest":
            return selftest()
    except (IndexError, ValueError) as exc:
        print(f"error: {exc}\n", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2
    print(USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
