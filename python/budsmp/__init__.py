"""budsmp — Galaxy Buds multipoint enabler.

Currently this package holds the protocol layer (`budsmp.frame`), which is shared
by the tooling and usable on its own for building and decoding frames. The
Linux/Windows RFCOMM transport and CLI land here too; on macOS the tool lives in
../macos (IOBluetooth has no usable Python binding, and the Bluetooth privacy
prompt requires an app bundle).
"""

__all__ = ["frame"]
__version__ = "1.0.0"
