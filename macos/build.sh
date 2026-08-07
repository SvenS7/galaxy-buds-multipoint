#!/usr/bin/env bash
# Build budsmp as a .app bundle.
#
# Why a .app and not a plain CLI binary?
#   Creating an IOBluetoothDevice spins up IOBluetoothCoreBluetoothCoordinator,
#   which is gated by TCC (the Bluetooth privacy prompt). A bare binary launched
#   from a shell inherits its launcher as the *responsible process*. If that
#   launcher has no NSBluetoothAlwaysUsageDescription — and terminals do not —
#   TCC kills the process with SIGABRT and never shows a prompt.
#
#   Packaging the binary in a .app that carries the usage description in its own
#   Info.plist, and launching it through LaunchServices (`open`), makes the app
#   its own responsible process. macOS then shows the normal prompt and
#   remembers the grant. The `budsmp` wrapper does exactly that.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

APP_NAME="BudsMP"
BUNDLE_ID="io.github.galaxy-buds-multipoint.budsmp"
USAGE_DESC="Talks to your own Galaxy Buds over Bluetooth SPP to enable multipoint."
APP="build/${APP_NAME}.app"

command -v swiftc >/dev/null 2>&1 || {
  echo "error: swiftc not found. Install the Xcode command line tools:" >&2
  echo "         xcode-select --install" >&2
  exit 1
}

mkdir -p build "${APP}/Contents/MacOS"

# Host arch by default. BUILD_UNIVERSAL=1 attempts a fat binary; if the second
# slice fails to compile the build falls back to the host-only binary.
build_slice () {  # <target-triple> <output>
  swiftc -O \
    -target "$1" \
    -framework IOBluetooth -framework AVFoundation -framework AudioToolbox \
    Sources/main.swift -o "$2"
}

BIN="${APP}/Contents/MacOS/${APP_NAME}"

if [[ "${BUILD_UNIVERSAL:-0}" == "1" ]]; then
  echo "== building universal =="
  ok_slices=()
  for triple in arm64-apple-macos11.0 x86_64-apple-macos11.0; do
    if build_slice "$triple" "build/${APP_NAME}-${triple%%-*}" 2>/dev/null; then
      ok_slices+=("build/${APP_NAME}-${triple%%-*}")
      echo "   ok: $triple"
    else
      echo "   skipped: $triple (no SDK slice)"
    fi
  done
  if [[ ${#ok_slices[@]} -gt 1 ]]; then
    lipo -create "${ok_slices[@]}" -output "$BIN"
  elif [[ ${#ok_slices[@]} -eq 1 ]]; then
    cp "${ok_slices[0]}" "$BIN"
  else
    echo "error: no slice built" >&2; exit 1
  fi
else
  echo "== building for $(uname -m) =="
  build_slice "$(uname -m)-apple-macos11.0" "$BIN"
fi

cat > "${APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>${APP_NAME}</string>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleName</key><string>${APP_NAME}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key><string>${USAGE_DESC}</string>
  <key>NSBluetoothPeripheralUsageDescription</key><string>${USAGE_DESC}</string>
</dict>
</plist>
PLIST

# Ad-hoc signature. TCC keys a grant to the code signature, so an unsigned
# bundle would re-prompt after every rebuild.
codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1 \
  || codesign --force --sign - "$APP"

echo "   built ${APP}"
echo
echo "Run it with the wrapper (it launches the app and prints the log):"
echo "   ./budsmp apply"
