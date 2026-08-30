"""Keeping the buds awake while the RFCOMM channel is opened.

The SPPSERVICE4 server only runs while the buds are awake; idle buds refuse the
connection no matter how many times you retry. Holding an audio stream open for
the duration of the attempt is enough to keep them up, so this plays a quiet
near-ultrasonic tone.

Only the stream matters, not the sound — and it only works if the buds are the
current output device, which is why the chosen player is logged.

Standard library only: the tone is synthesised into a temporary WAV and handed to
whatever player the system already has.
"""

from __future__ import annotations

import array
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave

DEFAULT_FREQ = 19_000.0
DEFAULT_AMP = 0.02
_CLIP_SECONDS = 5.0
_RATE = 48_000

# Ordered by how likely they are to reach the default output device rather than
# a specific card. Every one of these takes "play this file and exit".
_PLAYERS = [
    ["paplay"],                                  # PulseAudio / PipeWire-pulse
    ["pw-play"],                                 # PipeWire native
    ["aplay", "-q"],                             # ALSA
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
]


def _write_tone(path: str, freq: float, amp: float,
                seconds: float = _CLIP_SECONDS, rate: int = _RATE) -> None:
    peak = max(0, min(32767, int(amp * 32767)))
    step = 2.0 * math.pi * freq / rate
    frames = int(rate * seconds)
    # The clip is looped by restarting the player, whose gap is milliseconds
    # wide, so there is no point aligning the ends to a cycle boundary.
    samples = array.array("h", (int(peak * math.sin(step * n)) for n in range(frames)))
    if sys.byteorder == "big":
        samples.byteswap()                       # WAV is little-endian
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def _find_player() -> list[str] | None:
    import shutil
    for cmd in _PLAYERS:
        if shutil.which(cmd[0]):
            return cmd
    return None


class WakeTone:
    """Plays a looping tone until stopped. Never fatal: it either helps or warns."""

    def __init__(self, log, freq: float = DEFAULT_FREQ, amp: float = DEFAULT_AMP):
        self.log = log
        self.freq = freq
        self.amp = amp
        self._path: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._winsound = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        try:
            fd, path = tempfile.mkstemp(prefix="budsmp-wake-", suffix=".wav")
            os.close(fd)
            _write_tone(path, self.freq, self.amp)
            self._path = path
        except OSError as exc:
            self.log(f"wake tone: could not synthesise the clip ({exc}); continuing without it")
            return

        if sys.platform == "win32" and self._start_winsound():
            return
        player = _find_player()
        if player is None:
            self._cleanup_file()
            self.log("wake tone: no audio player found "
                     f"({', '.join(p[0] for p in _PLAYERS)})")
            self._manual_hint()
            return
        self._thread = threading.Thread(target=self._loop, args=(player,), daemon=True)
        self._thread.start()
        self.log(f"wake tone on ({int(self.freq)} Hz, amp {self.amp}) via {player[0]}")
        self._output_hint()

    def stop(self) -> None:
        self._stop.set()
        if self._winsound is not None:
            try:
                self._winsound.PlaySound(None, self._winsound.SND_PURGE)
            except RuntimeError:
                pass
            self._winsound = None
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._cleanup_file()

    def __enter__(self) -> "WakeTone":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- players -----------------------------------------------------------

    def _start_winsound(self) -> bool:
        try:
            import winsound
        except ImportError:
            return False
        try:
            winsound.PlaySound(self._path,
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        except RuntimeError as exc:
            self.log(f"wake tone: winsound refused the clip ({exc})")
            return False
        self._winsound = winsound
        self.log(f"wake tone on ({int(self.freq)} Hz, amp {self.amp}) via winsound")
        self._output_hint()
        return True

    def _loop(self, player: list[str]) -> None:
        assert self._path is not None
        cmd = [*player, self._path]
        # A player that returns instantly is not playing anything — usually a
        # misconfigured sink. Without this guard the loop would respawn it as
        # fast as the OS allows for the whole session.
        quick_exits = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(cmd,
                                              stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL)
            except OSError as exc:
                self.log(f"wake tone: {player[0]} would not start ({exc})")
                return
            self._proc.wait()
            if self._stop.is_set():
                return
            if self._proc.returncode not in (0, -15, 15):
                self.log(f"wake tone: {player[0]} exited {self._proc.returncode}; giving up on the tone")
                self._manual_hint()
                return
            if time.monotonic() - started >= _CLIP_SECONDS / 2:
                quick_exits = 0
                continue
            quick_exits += 1
            if quick_exits >= 3:
                self.log(f"wake tone: {player[0]} keeps returning immediately, so nothing "
                         "is reaching the output device; giving up on the tone")
                self._manual_hint()
                return
            self._stop.wait(0.5)

    # -- messaging ---------------------------------------------------------

    def _output_hint(self) -> None:
        self.log("  (the tone only wakes the buds if they are the current output device;")
        self.log("   if they are not, switch output and retry, or use --no-wake)")

    def _manual_hint(self) -> None:
        self.log("  wake the buds by hand instead: take them out of the case, select them")
        self.log("  as the output device, and start playing something before retrying")

    def _cleanup_file(self) -> None:
        if self._path is not None:
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self._path = None
