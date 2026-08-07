"""budsmp — Galaxy Buds multipoint enabler.

    python3 -m budsmp apply     # the fix, on Linux and Windows
    python3 -m budsmp --help

Layout:
    frame      the Samsung SMEP protocol: building frames, decoding device state
    transport  RFCOMM client sockets (Linux AF_BLUETOOTH, Windows AF_BTH)
    discover   paired devices and the SPPSERVICE4 channel, via the OS's own tools
    wake       the tone that keeps the buds' SPP server up while connecting
    cli        commands and options, mirroring the macOS tool exactly

`frame` is pure and dependency-free, so it is also useful on its own — including
on macOS, where the tool itself is a Swift program in ../macos (IOBluetooth has
no usable Python binding, and its privacy prompt requires an app bundle).

Requires Python 3.9 or newer. Nothing outside the standard library.
"""

__all__ = ["cli", "discover", "frame", "transport", "wake"]
__version__ = "1.0.0"
