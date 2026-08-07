"""budsmp — Galaxy Buds multipoint enabler for Linux and Windows.

Same commands, options and exit codes as the macOS tool in ../../macos, so the
documentation and the troubleshooting advice apply to all three. The protocol
lives in frame.py, the socket in transport.py, device lookup in discover.py.

Run it as `python3 -m budsmp <command>`, or through the wrappers in linux/ and
windows/.
"""

from __future__ import annotations

import os
import sys
import threading
from collections import Counter

from . import discover, frame, transport
from .transport import OpenFailed, Session, Unsupported
from .wake import DEFAULT_AMP, DEFAULT_FREQ, WakeTone

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_DEVICE = 2
EXIT_OPEN_FAILED = 3
EXIT_TIMEOUT = 4
EXIT_NOT_VERIFIED = 5

KNOWN_COMMANDS = ("apply", "revert", "read", "watch", "send", "scan", "sdp", "frame", "help")

USAGE = """budsmp — enable Galaxy Buds multipoint on a non-Galaxy host

USAGE
  budsmp <command> [options]

COMMANDS
  apply            Write asVer=2 so the buds stop tearing down the other device.
                   This is the fix. It persists in the buds' NVRAM.
  revert           Write asVer=0, restoring the stock account-gated behaviour.
  read             Read back the stored device state (asVer, account hashes).
                   Writes --asver first (default 2) to make the buds report.
  watch            Listen without writing anything, and report whatever the buds
                   push. The only command that measures the stored value rather
                   than one it just wrote — use it to test whether asVer survived
                   a reconnect or a power cycle.
  send <hex>...    Send raw SMEP frames, then listen.
  scan             List paired Bluetooth devices and their RFCOMM services.
  sdp              Query the target's SDP records and dump the channel map.
  frame            Print the frame `apply` would send, without sending it.

OPTIONS
  --addr <mac>     Target address. Default: first paired device matching --name.
  --name <text>    Name substring used to find the device (default "buds").
  --channel <n>    RFCOMM channel. Default: SPPSERVICE4 from SDP, else 29.
  --asver <n>      Version byte to write (default 2; the gate accepts 2 or 3).
  --account <hhhh> Also overwrite the stored account hash (not needed; research).
  --selector <n>   Account selector byte used with --account (default 1).
  --listen <sec>   Seconds to listen after sending (watch defaults to 45).
  --attempts <n>   RFCOMM open attempts (default 8).
  --retry <sec>    Delay between attempts (default 0.7).
  --no-wake        Skip the wake tone that keeps the buds' SPP server up.
  --wake-freq <hz> Wake tone frequency (default 19000).
  --wake-vol <0-1> Wake tone amplitude (default 0.02).
  --timeout <sec>  Hard timeout (default 60).
  --log <path>     Mirror output to this file.
  -h, --help       Show this text.

EXIT CODES
  0 ok   1 usage   2 device not found   3 rfcomm open failed
  4 timeout        5 sent but could not verify the new state
"""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class Log:
    """Everything goes to stderr, and to --log as well when asked."""

    def __init__(self, path: str | None = None):
        self.fh = None
        if path:
            try:
                parent = os.path.dirname(os.path.abspath(path))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                self.fh = open(path, "w", encoding="utf-8")
            except OSError as exc:
                print(f"warning: cannot write log {path}: {exc}", file=sys.stderr)

    def __call__(self, msg: str = "") -> None:
        print(msg, file=sys.stderr, flush=True)
        if self.fh is not None:
            self.fh.write(msg + "\n")
            self.fh.flush()


LOG = Log()


def finish(code: int, msg: str = "", tone: WakeTone | None = None) -> "NoReturn":  # noqa: F821
    if tone is not None:
        tone.stop()
    LOG(f"RESULT: OK {msg}" if code == EXIT_OK else f"RESULT: FAIL({code}) {msg}")
    sys.exit(code)


def usage_error(msg: str) -> "NoReturn":  # noqa: F821
    # stderr, not stdout: this is the failure path, and it has to interleave
    # correctly with the RESULT line that finish() writes.
    print(USAGE, end="", file=sys.stderr)
    finish(EXIT_USAGE, msg)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

class Options:
    def __init__(self):
        self.command = "apply"
        self.address: str | None = None
        self.name_needle = "buds"
        self.channel: int | None = None
        self.as_ver = 2
        self.account: int | None = None
        self.selector = 1
        self.raw_frames: list[str] = []
        self.log_path: str | None = None
        self.listen: float | None = None
        self.attempts = 8
        self.retry_delay = 0.7
        self.wake = True
        self.wake_freq = DEFAULT_FREQ
        self.wake_amp = DEFAULT_AMP
        self.timeout = 60.0
        # Whether --timeout was given. A long --listen otherwise trips the watchdog.
        self.timeout_explicit = False


def parse_options(args: list[str]) -> Options:
    o = Options()
    positionals: list[str] = []
    i = 0

    def value(flag: str) -> str:
        nonlocal i
        i += 1
        if i >= len(args):
            usage_error(f"missing value for {flag}")
        return args[i]

    def number(flag: str, cast, what: str):
        raw = value(flag)
        try:
            return cast(raw)
        except ValueError:
            usage_error(f"{flag} needs {what}, got {raw!r}")

    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(USAGE, end="")
            sys.exit(EXIT_OK)
        elif a in ("--addr", "--address"):
            o.address = value(a)
        elif a == "--name":
            o.name_needle = value(a)
        elif a == "--channel":
            o.channel = number(a, int, "a channel number")
        elif a == "--asver":
            o.as_ver = number(a, int, "a version number")
        elif a == "--account":
            raw = value(a).lower().removeprefix("0x")
            try:
                o.account = int(raw, 16)
            except ValueError:
                usage_error("--account needs 4 hex digits")
            if not 0 <= o.account <= 0xFFFF:
                usage_error("--account is a 2-byte value")
        elif a == "--selector":
            o.selector = number(a, int, "a selector byte")
        elif a == "--listen":
            o.listen = number(a, float, "seconds")
        elif a == "--attempts":
            o.attempts = number(a, int, "a count")
        elif a == "--retry":
            o.retry_delay = number(a, float, "seconds")
        elif a == "--no-wake":
            o.wake = False
        elif a == "--wake-freq":
            o.wake_freq = number(a, float, "a frequency in Hz")
        elif a == "--wake-vol":
            o.wake_amp = number(a, float, "an amplitude between 0 and 1")
        elif a == "--timeout":
            o.timeout = number(a, float, "seconds")
            o.timeout_explicit = True
        elif a == "--log":
            o.log_path = value(a)
        elif a.startswith("-"):
            usage_error(f"unknown option {a}")
        else:
            positionals.append(a)
        i += 1

    if not positionals:
        return o                                   # bare `budsmp` => apply
    if positionals[0] not in KNOWN_COMMANDS:
        usage_error(f'unknown command "{positionals[0]}"')
    o.command = positionals[0]
    o.raw_frames = positionals[1:]
    return o


def parse_hex(s: str) -> bytes:
    try:
        return bytes.fromhex(s.replace(" ", "").replace(":", ""))
    except ValueError:
        usage_error(f"not valid hex: {s}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _majority(values):
    """Most-seen value, which survives a truncated frame at the tail better than
    simply taking the last one."""
    counts = Counter(values)
    return counts.most_common(1)[0][0] if counts else None


def report_state(rx: bytes, expecting: int | None, tone: WakeTone | None,
                 wrote: bool = True) -> "NoReturn":  # noqa: F821
    states = frame.decode_state(rx)
    LOG()
    LOG("--- device state as reported by the buds --------------------------")
    if not states:
        LOG("  no 02 05 4c 0b state frame arrived.")
        if wrote:
            LOG("  the write may still have landed — re-run `budsmp read` with the buds")
            LOG("  awake, or start/stop playback to force the record to be re-evaluated.")
        else:
            LOG("  the buds only push their state when something makes them re-evaluate")
            LOG("  the record. Re-run and start or stop playback while it listens.")
        LOG("------------------------------------------------------------------")
        if expecting is not None:
            finish(EXIT_NOT_VERIFIED, "no state frame to verify against", tone)
        finish(EXIT_OK if wrote else EXIT_NOT_VERIFIED, "no state frame", tone)

    as_ver = _majority(s.as_ver for s in states)
    declared = _majority(s.declared_account for s in states)
    peers = [a for a, _ in Counter(a for s in states for a in s.peer_accounts).most_common()]

    if not wrote:
        LOG("  (nothing was written — this is the stored value)")
    LOG(f"  state frames       : {len(states)}")
    verdict = "[multipoint allowed]" if frame.multipoint_allowed(as_ver) else "[multipoint blocked]"
    LOG(f"  asVer (this host)  : {as_ver}   {verdict}")
    none_note = "   [none — expected, and fine]" if declared == 0 else ""
    LOG(f"  account declared   : 0x{declared:04x}{none_note}")
    if peers:
        LOG("  account of peer(s) : " + ", ".join(f"0x{a:04x}" for a in peers))
    LOG("------------------------------------------------------------------")

    if expecting is None:
        finish(EXIT_OK, "", tone)
    # The firmware normalises a written 0 up to 1, so only check the pass set.
    if as_ver == expecting or (expecting == 0 and as_ver <= 1):
        finish(EXIT_OK, f"asVer={as_ver}", tone)
    finish(EXIT_NOT_VERIFIED, f"wrote asVer={expecting} but the device reports {as_ver}", tone)


# ---------------------------------------------------------------------------
# Commands that need no session
# ---------------------------------------------------------------------------

def run_frame(o: Options) -> "NoReturn":  # noqa: F821
    if o.account is None:
        f = frame.version_only(o.as_ver)
        LOG(f"asVer={o.as_ver} (version-only)")
    else:
        f = frame.with_account(o.as_ver, o.selector, o.account)
        LOG(f"asVer={o.as_ver} account=0x{o.account:04x}")
    LOG(f.hex())
    finish(EXIT_OK, f.hex())


def run_scan() -> "NoReturn":  # noqa: F821
    devices = discover.paired_devices()
    LOG(f"paired devices: {len(devices)}")
    if not devices:
        LOG("  (none found — the OS helper may be missing; see the platform README)")
    for d in devices:
        LOG(f'  {d.address}  connected={"yes" if d.connected else "no "}  "{d.name}"')
    if devices and sys.platform.startswith("linux"):
        LOG("")
        LOG("run `budsmp sdp --addr <address>` for a device's RFCOMM channel map")
    finish(EXIT_OK, f"{len(devices)} paired device(s)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    global LOG
    args = list(sys.argv[1:] if argv is None else argv)

    # Open the log before parsing so usage errors land in it too.
    if "--log" in args:
        idx = args.index("--log")
        if idx + 1 < len(args):
            LOG = Log(args[idx + 1])

    o = parse_options(args)
    if o.log_path and LOG.fh is None:
        LOG = Log(o.log_path)

    if o.command == "help":
        print(USAGE, end="")
        return EXIT_OK

    LOG(f"=== budsmp {o.command} ===")

    if o.command == "frame":
        run_frame(o)                               # exits
    if o.command == "scan":
        run_scan()                                 # exits

    if not 0 <= o.as_ver <= 3:
        usage_error("--asver must be 0..3; the firmware rejects anything above 3")

    # Build the frames before going near the hardware, so bad input fails fast.
    if o.command == "apply":
        frames = [frame.version_only(o.as_ver) if o.account is None
                  else frame.with_account(o.as_ver, o.selector, o.account)]
        expecting: int | None = o.as_ver
        default_listen = 6.0
    elif o.command == "revert":
        frames = [frame.version_only(0)]
        expecting = 0
        default_listen = 6.0
    elif o.command == "read":
        # Re-writing the version byte the device already holds is a no-op that
        # still makes the buds re-evaluate the record and push their state
        # NOTIFY. Plain GETs never carry the account, so this nudge is the only
        # way to read it back from the host alone. See docs/protocol.md.
        frames = [frame.version_only(o.as_ver)]
        expecting = None
        default_listen = 10.0
    elif o.command == "watch":
        # Deliberately empty: every other command writes before it reports, so the
        # value it prints is one it just set. This one only ever observes.
        frames = []
        expecting = None
        default_listen = 45.0
    elif o.command == "send":
        if not o.raw_frames:
            usage_error("send needs at least one hex frame")
        frames = [parse_hex(h) for h in o.raw_frames]
        expecting = None
        default_listen = 10.0
    elif o.command == "sdp":
        frames, expecting, default_listen = [], None, 0.0
    else:
        usage_error(f'unknown command "{o.command}"')

    # Everything past here touches Bluetooth, so refuse now and clearly rather
    # than from inside a socket call after discovery has already run.
    if o.command != "sdp":
        try:
            transport.bluetooth_family()
        except Unsupported as exc:
            finish(EXIT_OPEN_FAILED, str(exc))

    device = discover.find_device(o.address, o.name_needle, LOG)
    if device is None:
        finish(EXIT_NO_DEVICE, "target device not found")
    LOG(f'device {device.address} "{device.name}" connected={device.connected}')

    if o.command == "sdp":
        shown = discover.describe_services(device.address, LOG)
        finish(EXIT_OK, f"{shown} service record(s)")

    seconds = o.listen if o.listen is not None else default_listen
    # A long --listen would otherwise be cut short by the default watchdog.
    deadline = o.timeout if o.timeout_explicit else max(o.timeout, seconds + 25)

    # A hard deadline, so a wedged Bluetooth stack cannot hang the tool forever.
    def bail():
        LOG(f"RESULT: FAIL({EXIT_TIMEOUT}) timed out after {deadline}s")
        os._exit(EXIT_TIMEOUT)

    watchdog = threading.Timer(deadline, bail)
    watchdog.daemon = True
    watchdog.start()

    tone: WakeTone | None = None
    nudge: threading.Timer | None = None
    try:
        channel = discover.resolve_channel(device.address, o.channel, LOG)
        if o.command == "watch":
            LOG("watch: sending nothing — whatever arrives is what the buds already hold")
        if o.wake:
            tone = WakeTone(LOG, o.wake_freq, o.wake_amp)
            tone.start()
        session = Session(device.address, channel, LOG,
                          attempts=o.attempts, retry_delay=o.retry_delay)
        try:
            session.open()
        except OpenFailed as exc:
            LOG("")
            LOG(f"could not open RFCOMM channel {channel}.")
            LOG("the buds only run their SPP server while awake — take them out of the")
            LOG("case, make them the audio output device, start playback, and retry.")
            finish(EXIT_OPEN_FAILED, str(exc), tone)
        try:
            session.send(frames)
            if seconds > 0:
                what = "sending nothing" if not frames else "frames sent"
                LOG(f"{what}; listening {seconds}s for device state ...")
            if o.command == "watch":
                # The buds push their state when a record is re-evaluated, and that
                # happens on audio-connection changes. Since we refuse to write,
                # ending the wake tone is the nudge — and if it is off, the user has
                # to play or pause something instead.
                if tone is None:
                    LOG("  start or stop playback now to make the buds re-evaluate the record")
                else:
                    def drop_tone():
                        LOG("watch: stopping the wake tone — the audio change should trigger a report")
                        LOG("  (if nothing arrives, start or stop playback while this listens)")
                        tone.stop()

                    nudge = threading.Timer(2.0, drop_tone)
                    nudge.daemon = True
                    nudge.start()
            rx = session.listen(seconds)
        finally:
            if nudge is not None:
                nudge.cancel()
            session.close()
        report_state(rx, expecting, tone, wrote=bool(frames))   # exits
    except Unsupported as exc:
        finish(EXIT_OPEN_FAILED, str(exc), tone)
    except KeyboardInterrupt:
        finish(EXIT_TIMEOUT, "interrupted", tone)


if __name__ == "__main__":
    raise SystemExit(main())
