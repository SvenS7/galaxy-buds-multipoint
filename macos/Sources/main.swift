// budsmp — Galaxy Buds multipoint enabler (macOS / IOBluetooth)
//
// Opens the buds' SPPSERVICE4 RFCOMM channel and writes a Samsung SMEP
// MDE_VERSION frame that sets this host's `asVer` field to 2, which is the only
// gate the firmware actually checks before letting a second device coexist on
// the link. See ../../docs/protocol.md for the wire format and
// ../../docs/firmware-gate.md for why this works.
//
// Everything is discovery-driven: the device is located among the paired devices
// by name and the RFCOMM channel number comes from the device's own SDP records.
// No addresses or account values are hard-coded.

import Foundation
import IOBluetooth
import AVFoundation
import AudioToolbox

// ---------------------------------------------------------------------------
// MARK: - Logging
// ---------------------------------------------------------------------------

// The tool normally runs inside a .app bundle launched by LaunchServices (see
// build.sh for why), so stderr is not visible in the terminal. Everything is
// mirrored into a log file that the `budsmp` wrapper prints on exit.
final class Logger {
    private var fh: FileHandle?
    private let q = DispatchQueue(label: "budsmp.log")

    /// Prefix every line with a wall-clock time. Off for one-shot commands, whose
    /// output the wrapper prints immediately; on for the daemon, where a line's
    /// only useful context is when it happened.
    var stamped = false
    private lazy var clock: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MM-dd HH:mm:ss"
        return f
    }()

    init(path: String?) {
        guard let path = path else { return }
        let dir = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        fh = FileHandle(forWritingAtPath: path)
        // Append rather than truncate: the daemon runs under launchd with
        // KeepAlive, and wiping the log on every restart would throw away the
        // history `install-agent.sh status` exists to show. One-shot runs still
        // show only their own output, because the budsmp wrapper empties its log
        // before launching the app.
        fh?.seekToEndOfFile()
    }

    func write(_ s: String) {
        q.sync {
            let line = stamped ? "\(clock.string(from: Date()))  \(s)\n" : s + "\n"
            guard let d = line.data(using: .utf8) else { return }
            fh?.write(d)
            FileHandle.standardError.write(d)
        }
    }
}

var LOG = Logger(path: nil)
func log(_ s: String) { LOG.write(s) }

enum Exit: Int32 {
    case ok = 0
    case usage = 1
    case noDevice = 2
    case openFailed = 3
    case timeout = 4
    case notVerified = 5
}

func finish(_ code: Exit, _ msg: String = "") -> Never {
    tone?.stop()
    log(code == .ok ? "RESULT: OK \(msg)" : "RESULT: FAIL(\(code.rawValue)) \(msg)")
    exit(code.rawValue)
}

// ---------------------------------------------------------------------------
// MARK: - Hex helpers
// ---------------------------------------------------------------------------

func hex(_ b: [UInt8]) -> String { b.map { String(format: "%02x", $0) }.joined() }
func hex(_ d: Data) -> String { d.map { String(format: "%02x", $0) }.joined() }
func ioHex(_ v: IOReturn) -> String { String(format: "0x%08x", UInt32(bitPattern: v)) }

func hexToBytes(_ s: String) -> [UInt8]? {
    let clean = s.replacingOccurrences(of: " ", with: "")
                 .replacingOccurrences(of: ":", with: "")
                 .lowercased()
    guard !clean.isEmpty, clean.count % 2 == 0 else { return nil }
    var out = [UInt8]()
    var i = clean.startIndex
    while i < clean.endIndex {
        let j = clean.index(i, offsetBy: 2)
        guard let b = UInt8(clean[i..<j], radix: 16) else { return nil }
        out.append(b)
        i = j
    }
    return out
}

// ---------------------------------------------------------------------------
// MARK: - SMEP framing
// ---------------------------------------------------------------------------
//
//   FC | hdr1 hdr2 | 01 | msgID | payload... | CRC16(2, LE) | CC
//
//   i2 = (hdr2 << 8) | hdr1
//        bits 0-9 : length of the region starting at byte[3], i.e.
//                   fixed(1) + msgID(1) + payload + CRC(2)
//        bit 12   : response, bit 13: fragment, bits 14-15: sequence
//   CRC = CRC16-CCITT/XMODEM (poly 0x1021, init 0x0000) over packet[3 ..< len-3]

let SMEP_SOM: UInt8 = 0xFC
let SMEP_EOM: UInt8 = 0xCC
let MSG_SET: UInt8 = 0x43          // write attribute
let MSG_NOTIFY: UInt8 = 0x45       // pushed device state
let MDE_VERSION_OPCODE: UInt8 = 0x0b

func crc16Xmodem(_ data: [UInt8]) -> UInt16 {
    var crc: UInt16 = 0x0000
    for byte in data {
        crc ^= UInt16(byte) << 8
        for _ in 0..<8 {
            crc = (crc & 0x8000) != 0 ? (crc << 1) ^ 0x1021 : crc << 1
        }
    }
    return crc
}

func smepBuild(msgID: UInt8, payload: [UInt8], seq: UInt8 = 0) -> [UInt8] {
    let plen = 1 + 1 + payload.count + 2
    let i2 = (plen & 0x3ff) | ((Int(seq) & 3) << 14)
    let core = [0x01, msgID] + payload
    let crc = crc16Xmodem(core)
    return [SMEP_SOM, UInt8(i2 & 0xff), UInt8((i2 >> 8) & 0xff)]
        + core
        + [UInt8(crc & 0xff), UInt8((crc >> 8) & 0xff), SMEP_EOM]
}

/// MDE_VERSION SET carrying only the version byte. Leaves the stored account
/// field untouched — this is the frame the fix actually needs.
func mdeVersionOnly(_ version: UInt8) -> [UInt8] {
    let blob: [UInt8] = [0x00, 0x00, MDE_VERSION_OPCODE, version]
    return smepBuild(msgID: MSG_SET, payload: [0x04, 0x03, UInt8(blob.count)] + blob)
}

/// MDE_VERSION SET that also overwrites the stored account hash. Not required
/// for multipoint (see docs/experiments.md) — kept for protocol research.
func mdeWithAccount(version: UInt8, selector: UInt8, hash: UInt16) -> [UInt8] {
    let blob: [UInt8] = [0x00, 0x00, MDE_VERSION_OPCODE, version, selector,
                         UInt8((hash >> 8) & 0xff), UInt8(hash & 0xff)]
    return smepBuild(msgID: MSG_SET, payload: [0x04, 0x03, UInt8(blob.count)] + blob)
}

struct SmepFrame {
    let msgID: UInt8
    let payload: [UInt8]
}

/// Split a raw RFCOMM byte stream into SMEP frames using the length header.
func smepParse(_ s: [UInt8]) -> [SmepFrame] {
    var out = [SmepFrame]()
    var i = 0
    while i < s.count {
        guard s[i] == SMEP_SOM else { i += 1; continue }
        guard i + 3 < s.count else { break }
        let i2 = (Int(s[i + 2]) << 8) | Int(s[i + 1])
        let total = (i2 & 0x3ff) + 4
        guard total >= 8, i + total <= s.count, s[i + total - 1] == SMEP_EOM else { i += 1; continue }
        let raw = Array(s[i ..< i + total])
        out.append(SmepFrame(msgID: raw[4], payload: Array(raw[5 ..< raw.count - 3])))
        i += total
    }
    return out
}

// ---------------------------------------------------------------------------
// MARK: - Device-state decoding
// ---------------------------------------------------------------------------
//
// The buds push a NOTIFY (msgID 0x45) whose payload starts with 02 05 4c 0b
// whenever a peer record is re-evaluated. It reports, in the clear:
//   offset 6            : asVer currently stored for this host
//   offset 8-9          : account hash this host declared     (little-endian)
//   "eb 1a 00" + 2 bytes: account hash of the other peer      (little-endian)

struct DeviceState {
    /// Every asVer seen, in arrival order. A write produces frames for the record
    /// as it was *and* as it now is, so this is a before/after trace rather than
    /// a set of readings to average.
    var asVerSeen: [UInt8] = []
    var declaredSeen: [UInt16] = []
    var peerAccounts: [UInt16] = []
    var frameCount = 0

    /// The newest report. Frames arrive in order on a reliable stream, and a
    /// truncated one fails to decode rather than contributing a stale value, so
    /// the last decoded frame is the current state.
    var asVer: UInt8? { asVerSeen.last }
    var declaredAccount: UInt16? { declaredSeen.last }
    /// True when the frames disagree — normal right after a write.
    var asVerChanged: Bool { Set(asVerSeen).count > 1 }
}

func decodeState(_ rx: [UInt8]) -> DeviceState {
    var st = DeviceState()
    var peerVotes = [UInt16: Int]()

    for f in smepParse(rx) {
        let p = f.payload
        guard p.count >= 10, p[0] == 0x02, p[1] == 0x05, p[2] == 0x4c, p[3] == MDE_VERSION_OPCODE else { continue }
        st.frameCount += 1
        st.asVerSeen.append(p[6])
        st.declaredSeen.append(UInt16(p[9]) << 8 | UInt16(p[8]))
        var k = 0
        while k + 4 < p.count {
            if p[k] == 0xeb && p[k + 1] == 0x1a && p[k + 2] == 0x00 {
                peerVotes[UInt16(p[k + 4]) << 8 | UInt16(p[k + 3]), default: 0] += 1
            }
            k += 1
        }
    }
    st.peerAccounts = peerVotes.sorted { $0.value > $1.value }.map { $0.key }
    return st
}

// ---------------------------------------------------------------------------
// MARK: - Wake tone
// ---------------------------------------------------------------------------
//
// SPPSERVICE4's RFCOMM server is only up while the buds are awake. When they are
// idle, openRFCOMMChannelSync fails with kIOReturnError (0xe00002bc) no matter
// how often you retry. Holding an audio stream open keeps them awake for the
// duration of the attempt. The tone is near-ultrasonic and very quiet, but it
// only helps if the buds are the default output device — so the current default
// is logged, which makes that failure mode obvious.

func defaultOutputDeviceName() -> String? {
    var deviceID = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: 0)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &addr, 0, nil, &size, &deviceID) == noErr else { return nil }
    var name: Unmanaged<CFString>?
    var nameSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    var nameAddr = AudioObjectPropertyAddress(
        mSelector: kAudioObjectPropertyName,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: 0)
    guard AudioObjectGetPropertyData(deviceID, &nameAddr, 0, nil, &nameSize, &name) == noErr,
          let cfName = name?.takeRetainedValue() else { return nil }
    return cfName as String
}

final class WakeTone {
    private let engine = AVAudioEngine()
    private var phase: Double = 0
    private var started = false
    private let freq: Double
    private let amp: Float

    init(freq: Double, amp: Float) {
        self.freq = freq
        self.amp = amp
    }

    func start() {
        guard !started else { return }
        let output = defaultOutputDeviceName() ?? "(unknown)"
        let reported = engine.outputNode.outputFormat(forBus: 0).sampleRate
        let rate = reported > 0 ? reported : 48_000
        guard let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 2) else { return }
        let increment = 2.0 * Double.pi * freq / rate

        let source = AVAudioSourceNode { [weak self] _, _, frameCount, audioBufferList -> OSStatus in
            guard let self = self else { return noErr }
            let buffers = UnsafeMutableAudioBufferListPointer(audioBufferList)
            for frame in 0 ..< Int(frameCount) {
                let value = Float(sin(self.phase)) * self.amp
                self.phase += increment
                if self.phase > 2.0 * Double.pi { self.phase -= 2.0 * Double.pi }
                for buffer in buffers {
                    buffer.mData?.assumingMemoryBound(to: Float.self)[frame] = value
                }
            }
            return noErr
        }

        engine.attach(source)
        engine.connect(source, to: engine.mainMixerNode, format: format)
        engine.prepare()
        do {
            try engine.start()
            started = true
            log("wake tone on (\(Int(freq)) Hz, amp \(amp)) — default output is \"\(output)\"")
            log("  (if that is not the buds, the tone cannot wake them: switch output, or use --no-wake)")
        } catch {
            log("wake tone failed to start: \(error.localizedDescription)")
        }
    }

    func stop() {
        guard started else { return }
        engine.stop()
        started = false
    }
}

var tone: WakeTone?

// ---------------------------------------------------------------------------
// MARK: - Device / channel discovery
// ---------------------------------------------------------------------------

// SPPSERVICE4 as advertised by Galaxy Buds ("SPP4_MobileSettings" in firmware).
let SPP4_SERVICE_NAME = "SPPSERVICE4"
let SPP4_FALLBACK_CHANNEL: BluetoothRFCOMMChannelID = 29

func pairedDevices() -> [IOBluetoothDevice] {
    (IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice]) ?? []
}

func serviceRecords(_ dev: IOBluetoothDevice) -> [IOBluetoothSDPServiceRecord] {
    (dev.services as? [IOBluetoothSDPServiceRecord]) ?? []
}

/// IOBluetooth reports addresses as `xx-xx-…` while everyone types `XX:XX:…`,
/// so compare them stripped of both separators.
func normalizedAddress(_ s: String?) -> String {
    (s ?? "").lowercased()
        .replacingOccurrences(of: ":", with: "")
        .replacingOccurrences(of: "-", with: "")
}

func rfcommChannel(of record: IOBluetoothSDPServiceRecord) -> BluetoothRFCOMMChannelID? {
    var id: BluetoothRFCOMMChannelID = 0
    return record.getRFCOMMChannelID(&id) == kIOReturnSuccess ? id : nil
}

func findDevice(address: String?, nameNeedle: String) -> IOBluetoothDevice? {
    if let address = address {
        guard let dev = IOBluetoothDevice(addressString: address) else {
            log("not a usable Bluetooth address: \(address)")
            return nil
        }
        return dev
    }
    let needle = nameNeedle.lowercased()
    let matches = pairedDevices().filter { ($0.name ?? "").lowercased().contains(needle) }
    if matches.isEmpty {
        log("no paired device whose name contains \"\(nameNeedle)\"")
        log("run `budsmp scan` to list paired devices, then pass --addr <XX:XX:XX:XX:XX:XX>")
        return nil
    }
    if matches.count > 1 {
        log("note: \(matches.count) paired devices match \"\(nameNeedle)\"; preferring a connected one")
    }
    return matches.first(where: { $0.isConnected() }) ?? matches.first
}

func resolveChannel(_ dev: IOBluetoothDevice, override: BluetoothRFCOMMChannelID?) -> BluetoothRFCOMMChannelID {
    if let override = override {
        log("using RFCOMM channel \(override) (from --channel)")
        return override
    }
    for r in serviceRecords(dev) where (r.getServiceName() ?? "") == SPP4_SERVICE_NAME {
        if let ch = rfcommChannel(of: r) {
            log("resolved \(SPP4_SERVICE_NAME) -> RFCOMM channel \(ch) from SDP")
            return ch
        }
    }
    log("\(SPP4_SERVICE_NAME) not in cached SDP records; falling back to channel \(SPP4_FALLBACK_CHANNEL)")
    log("  (run `budsmp sdp` to refresh the SDP cache if this is wrong)")
    return SPP4_FALLBACK_CHANNEL
}

// ---------------------------------------------------------------------------
// MARK: - RFCOMM session
// ---------------------------------------------------------------------------

final class Session: NSObject, IOBluetoothRFCOMMChannelDelegate {
    private let device: IOBluetoothDevice
    private let channelID: BluetoothRFCOMMChannelID
    private let frames: [[UInt8]]
    private let listenSeconds: Double
    private let attempts: Int
    private let retryDelay: Double
    private let onDone: ([UInt8]) -> Void
    /// Runs once the channel is open and every frame has gone out. `watch` uses
    /// it to drop the wake tone, which is itself the event the buds react to.
    private let onListening: (() -> Void)?
    /// What to do when the channel cannot be opened. The one-shot commands exit
    /// with a status; the daemon logs and waits for the next connect event, so it
    /// passes its own handler.
    private let onFailure: ((Exit, String) -> Void)?

    private var channel: IOBluetoothRFCOMMChannel?
    private var rx = [UInt8]()
    private var sendIndex = 0
    private var completed = false

    init(device: IOBluetoothDevice,
         channelID: BluetoothRFCOMMChannelID,
         frames: [[UInt8]],
         listenSeconds: Double,
         attempts: Int,
         retryDelay: Double,
         onListening: (() -> Void)? = nil,
         onFailure: ((Exit, String) -> Void)? = nil,
         onDone: @escaping ([UInt8]) -> Void) {
        self.device = device
        self.channelID = channelID
        self.frames = frames
        self.listenSeconds = listenSeconds
        self.attempts = attempts
        self.retryDelay = retryDelay
        self.onListening = onListening
        self.onFailure = onFailure
        self.onDone = onDone
    }

    func start() {
        log("device \(device.addressString ?? "?") \"\(device.name ?? "?")\" connected=\(device.isConnected())")

        // RFCOMM needs the baseband ACL link up first.
        let rc = device.openConnection()
        log("openConnection => \(ioHex(rc)) connected=\(device.isConnected())")

        let total = max(1, attempts)
        for attempt in 1...total {
            var ch: IOBluetoothRFCOMMChannel?
            let res = device.openRFCOMMChannelSync(&ch, withChannelID: channelID, delegate: self)
            log("open ch\(channelID) attempt \(attempt)/\(total) => \(ioHex(res))")
            if res == kIOReturnSuccess, let ch = ch {
                channel = ch
                log("channel \(channelID) OPEN, MTU=\(ch.getMTU())")
                sendNext()
                return
            }
            if attempt < total { usleep(useconds_t(retryDelay * 1_000_000)) }
        }

        log("")
        log("could not open RFCOMM channel \(channelID) after \(total) attempts.")
        if onFailure == nil {
            log("the buds only run their SPP server while awake — take them out of the")
            log("case, make them the audio output device, start playback, and retry.")
        }
        fail(.openFailed, "rfcomm open failed after \(total) attempts")
    }

    /// Exit, unless a caller asked to be told instead.
    private func fail(_ code: Exit, _ msg: String) {
        guard let onFailure = onFailure else { finish(code, msg) }
        onFailure(code, msg)
    }

    func rfcommChannelOpenComplete(_ ch: IOBluetoothRFCOMMChannel!, status error: IOReturn) {
        log("openComplete status=\(ioHex(error))")
    }

    private func sendNext() {
        guard sendIndex < frames.count else {
            if listenSeconds > 0 {
                let what = frames.isEmpty ? "sending nothing" : "frames sent"
                log("\(what); listening \(listenSeconds)s for device state ...")
            }
            onListening?()
            DispatchQueue.main.asyncAfter(deadline: .now() + listenSeconds) { self.complete() }
            return
        }
        var bytes = frames[sendIndex]
        let res = channel?.writeSync(&bytes, length: UInt16(bytes.count)) ?? kIOReturnNotOpen
        log("TX[\(sendIndex)] \(bytes.count)B \(hex(bytes)) => \(ioHex(res))")
        sendIndex += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { self.sendNext() }
    }

    func rfcommChannelData(_ ch: IOBluetoothRFCOMMChannel!, data d: UnsafeMutableRawPointer!, length l: Int) {
        let buf = Data(bytes: d, count: l)
        rx.append(contentsOf: buf)
        log("RX \(l)B \(hex(buf))")
    }

    func rfcommChannelClosed(_ ch: IOBluetoothRFCOMMChannel!) {
        // Nothing more can arrive, so report now instead of sitting out the rest
        // of the listen window. This is the normal ending when the buds drop this
        // host — which is exactly what a failing gate looks like.
        log("channel closed by peer")
        complete()
    }

    private func complete() {
        guard !completed else { return }
        completed = true
        channel?.close()
        log("total RX \(rx.count)B")
        onDone(rx)
    }
}

// ---------------------------------------------------------------------------
// MARK: - Daemon
// ---------------------------------------------------------------------------
//
// The buds clear `asVer` every time they power down (docs/experiments.md), so the
// fix is a per-power-session command rather than a one-off. Every power session
// begins with a connection, so re-sending the frame on connect covers all of them
// without anyone having to remember. IOBluetooth delivers connect events to any
// process running a run loop, which is why this needs no polling.

/// Connect notifications fire as soon as the ACL link is up, which is a moment
/// before the buds' SPP server will accept anything.
let DAEMON_CONNECT_DELAY = 2.5

/// How long to wait for the Bluetooth authorization check inside IOBluetooth
/// before concluding that nothing is going to answer it. Generous, because a cold
/// bluetoothd can take a few seconds on its own.
let DAEMON_TCC_PATIENCE = 20.0

final class Daemon: NSObject {
    private let opts: Options
    private let wake: Bool

    private var connectNote: IOBluetoothUserNotification?
    private var disconnectNotes = [String: IOBluetoothUserNotification]()
    /// The RFCOMM open is synchronous and the buds run a single SPPSERVICE4
    /// server, so only one write may be in flight.
    private var running = false
    private var session: Session?
    private var applySeq = 0
    private var pending = Set<String>()
    private var appliedAt = [String: Date]()

    /// Set once the (potentially blocking) connect-notification registration has
    /// returned. Read from the watchdog queue, so it is guarded by `flagLock`.
    private var registeredFlag = false
    private let flagLock = NSLock()
    private var registered: Bool {
        get { flagLock.lock(); defer { flagLock.unlock() }; return registeredFlag }
        set { flagLock.lock(); registeredFlag = newValue; flagLock.unlock() }
    }

    init(opts: Options, wake: Bool) {
        self.opts = opts
        self.wake = wake
    }

    func start() {
        let target = opts.address.map { "address \($0)" }
            ?? "a paired device whose name contains \"\(opts.nameNeedle)\""
        log("daemon: writing asVer=\(opts.asVer) whenever \(target) connects")
        log("daemon: wake tone \(wake ? "on" : "off"), debounce \(opts.debounce)s")

        // Registering brings up IOBluetoothCoreBluetoothCoordinator, which blocks
        // on a semaphore until TCC has an answer about Bluetooth access. Launched
        // by launchd there is nobody to answer a prompt, so instead of returning a
        // failure this call simply never comes back. Say so from another queue
        // rather than hanging in silence, then exit: launchd's KeepAlive brings the
        // daemon back once the grant exists, which is the self-healing path.
        armBluetoothWatchdog()
        connectNote = IOBluetoothDevice.register(forConnectNotifications: self,
                                                selector: #selector(deviceConnected(_:device:)))
        registered = true
        guard connectNote != nil else {
            finish(.openFailed, "could not register for Bluetooth connect notifications")
        }

        // Installing the agent, logging in, or restarting the daemon all happen
        // while the buds may already be connected — that connect event is gone.
        for d in pairedDevices() where matches(d) && d.isConnected() {
            log("daemon: \(label(d)) is already connected")
            schedule(d, after: 1.0)
        }
        log("daemon: ready")
    }

    /// The main thread is about to be unavailable, so this has to run elsewhere.
    private func armBluetoothWatchdog() {
        DispatchQueue.global().asyncAfter(deadline: .now() + DAEMON_TCC_PATIENCE) { [weak self] in
            guard let self = self, !self.registered else { return }
            // One call per line, so the stamp lands on both.
            log("daemon: Bluetooth access has not been granted to this build, "
                + "and a background agent cannot ask for it.")
            log("daemon: run ./budsmp apply in a terminal, click Allow, then "
                + "./install-agent.sh — a rebuild needs the prompt answered again.")
            finish(.openFailed, "no Bluetooth access; exiting so launchd can retry")
        }
    }

    // MARK: notifications

    @objc private func deviceConnected(_ note: IOBluetoothUserNotification!, device: IOBluetoothDevice!) {
        // Every device on the machine is announced here, not just the buds.
        guard let device = device, matches(device) else { return }
        log("daemon: connected — \(label(device))")
        schedule(device, after: DAEMON_CONNECT_DELAY)
    }

    @objc private func deviceDisconnected(_ note: IOBluetoothUserNotification!, device: IOBluetoothDevice!) {
        note?.unregister()
        guard let device = device else { return }
        let key = key(device)
        disconnectNotes[key] = nil
        // The debounce is there to collapse duplicate connect notifications, not
        // to throttle real reconnections: a trip to the case can be over in
        // seconds, and that is exactly when the frame needs re-sending.
        appliedAt[key] = nil
        log("daemon: disconnected — \(label(device))")
    }

    // MARK: matching

    private func key(_ dev: IOBluetoothDevice) -> String { normalizedAddress(dev.addressString) }

    private func label(_ dev: IOBluetoothDevice) -> String {
        "\"\(dev.name ?? "?")\" \(dev.addressString ?? "?")"
    }

    private func matches(_ dev: IOBluetoothDevice) -> Bool {
        if let want = opts.address {
            return !want.isEmpty && normalizedAddress(dev.addressString) == normalizedAddress(want)
        }
        return (dev.name ?? "").lowercased().contains(opts.nameNeedle.lowercased())
    }

    // MARK: applying

    private func schedule(_ dev: IOBluetoothDevice, after delay: Double) {
        let k = key(dev)
        watchForDisconnect(dev, key: k)
        guard !pending.contains(k) else { return }
        if let last = appliedAt[k] {
            let ago = Date().timeIntervalSince(last)
            if ago < opts.debounce {
                log("daemon: skipping — already written \(String(format: "%.0f", ago))s ago")
                return
            }
        }
        pending.insert(k)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { self.apply(dev, key: k) }
    }

    private func watchForDisconnect(_ dev: IOBluetoothDevice, key: String) {
        guard disconnectNotes[key] == nil else { return }
        disconnectNotes[key] = dev.register(forDisconnectNotification: self,
                                            selector: #selector(deviceDisconnected(_:device:)))
    }

    private func apply(_ dev: IOBluetoothDevice, key k: String, connectWait: Int = 3) {
        pending.remove(k)
        if running {
            log("daemon: another write is in flight; retrying in 3s")
            schedule(dev, after: 3.0)
            return
        }
        guard dev.isConnected() else {
            // Registering for connect notifications also delivers one for devices
            // that were already connected, and a real event can land a beat
            // before the link is up, so give it a moment before deciding.
            guard connectWait > 0 else {
                log("daemon: \(label(dev)) is not connected after all; nothing to write")
                return
            }
            pending.insert(k)
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                self.apply(dev, key: k, connectWait: connectWait - 1)
            }
            return
        }

        running = true
        applySeq += 1
        let seq = applySeq

        if wake {
            tone = WakeTone(freq: opts.wakeFreq, amp: opts.wakeAmp)
            tone?.start()
        }

        let want = opts.asVer
        let budget = opts.timeout
        let frame = opts.account.map { mdeWithAccount(version: want, selector: opts.selector, hash: $0) }
            ?? mdeVersionOnly(want)
        // A connect event is precisely when the SPP server may still be coming
        // up, so the daemon is more patient than the one-shot commands.
        let s = Session(device: dev,
                        channelID: resolveChannel(dev, override: opts.channel),
                        frames: [frame],
                        listenSeconds: opts.listenSeconds ?? 4.0,
                        attempts: max(opts.attempts, 15),
                        retryDelay: opts.retryDelay,
                        onFailure: { [weak self] code, msg in
                            self?.done(k, seq: seq, code, msg)
                        }) { [weak self] rx in
            let (code, msg) = describeState(rx, expecting: want, wrote: true)
            self?.done(k, seq: seq, code, msg)
        }
        session = s

        // Nothing else would ever release `running` if a session wedged.
        DispatchQueue.main.asyncAfter(deadline: .now() + budget) { [weak self] in
            self?.done(k, seq: seq, .timeout, "gave up after \(budget)s")
        }
        s.start()
    }

    private func done(_ k: String, seq: Int, _ code: Exit, _ msg: String) {
        guard running, seq == applySeq else { return }
        running = false
        session = nil
        tone?.stop()
        tone = nil

        switch code {
        case .ok:
            appliedAt[k] = Date()
            log("daemon: written and verified — \(msg)")
        case .notVerified:
            // The frame went out; only the confirming NOTIFY did not arrive in
            // time. Re-sending it on the next connect is harmless either way.
            appliedAt[k] = Date()
            log("daemon: written but not confirmed — \(msg)")
        default:
            log("daemon: failed — \(msg); waiting for the next connect")
        }
        log("")
    }
}

// ---------------------------------------------------------------------------
// MARK: - Options
// ---------------------------------------------------------------------------

struct Options {
    var command = "apply"
    var address: String?
    var nameNeedle = "buds"
    var channel: BluetoothRFCOMMChannelID?
    var asVer: UInt8 = 2
    var account: UInt16?
    var selector: UInt8 = 1
    var rawFrames: [String] = []
    var logPath: String?
    var listenSeconds: Double?
    var attempts = 8
    var retryDelay = 0.7
    var wake = true
    /// Whether the wake tone was asked for either way. `daemon` leaves it off by
    /// default — the buds are awake by definition when they have just connected,
    /// and a background process should not seize the audio device.
    var wakeExplicit = false
    var wakeFreq = 19_000.0
    var wakeAmp: Float = 0.02
    var timeout = 60.0
    /// Whether --timeout was given. A long --listen otherwise trips the watchdog.
    var timeoutExplicit = false
    /// `daemon` only: how long a successful write suppresses another one.
    var debounce = 8.0
}

let usage = """
budsmp — enable Galaxy Buds multipoint on a non-Galaxy host (macOS)

USAGE
  budsmp <command> [options]

COMMANDS
  apply            Write asVer=2 so the buds stop tearing down the other device.
                   This is the fix. It survives disconnects, but not the buds
                   powering down — run it again after they have been in the case.
  daemon           Stay running and do the above every time the buds connect, so
                   you never have to think about the power-cycle problem again.
                   install-agent.sh sets this up to start at login.
  revert           Write asVer=0, restoring the stock account-gated behaviour.
  read             Read back the stored device state (asVer, account hashes).
                   Writes --asver first (default 2) to make the buds report.
  watch            Listen without writing anything and report whatever the buds
                   push. On the firmware we tested they only report around a
                   write, so this often sees nothing — `apply` is what reveals
                   the stored value. See docs/experiments.md.
  send <hex>...    Send raw SMEP frames, then listen.
  scan             List paired Bluetooth devices and their RFCOMM services.
  sdp              Run a fresh SDP query on the target and dump the channel map.
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
  --wake           Use the wake tone even in daemon mode, where it is off.
  --wake-freq <hz> Wake tone frequency (default 19000).
  --wake-vol <0-1> Wake tone amplitude (default 0.02).
  --timeout <sec>  Hard timeout (default 60; per write in daemon mode).
  --debounce <sec> daemon: ignore a repeat connect this soon after a write
                   (default 8). A disconnect clears it regardless.
  --log <path>     Mirror output to this file.
  -h, --help       Show this text.

EXIT CODES
  0 ok   1 usage   2 device not found   3 rfcomm open failed
  4 timeout        5 sent but could not verify the new state
"""

let KNOWN_COMMANDS: Set<String> = ["apply", "daemon", "revert", "read", "watch",
                                   "send", "scan", "sdp", "frame", "help"]

func parseOptions(_ args: [String]) -> Options {
    var o = Options()
    var positionals = [String]()

    func value(_ i: inout Int, _ flag: String) -> String {
        i += 1
        guard i < args.count else { finish(.usage, "missing value for \(flag)") }
        return args[i]
    }

    var i = 0
    while i < args.count {
        let a = args[i]
        switch a {
        case "-h", "--help":
            print(usage)
            exit(0)
        case "--addr", "--address": o.address = value(&i, a)
        case "--name":              o.nameNeedle = value(&i, a)
        case "--channel":           o.channel = BluetoothRFCOMMChannelID(value(&i, a)) ?? SPP4_FALLBACK_CHANNEL
        case "--asver":             o.asVer = UInt8(value(&i, a)) ?? 2
        case "--account":
            let raw = value(&i, a).replacingOccurrences(of: "0x", with: "")
            guard let parsed = UInt16(raw, radix: 16) else { finish(.usage, "--account needs 4 hex digits") }
            o.account = parsed
        case "--selector":          o.selector = UInt8(value(&i, a)) ?? 1
        case "--listen":            o.listenSeconds = Double(value(&i, a))
        case "--attempts":          o.attempts = Int(value(&i, a)) ?? 8
        case "--retry":             o.retryDelay = Double(value(&i, a)) ?? 0.7
        case "--no-wake":           o.wake = false; o.wakeExplicit = true
        case "--wake":              o.wake = true;  o.wakeExplicit = true
        case "--wake-freq":         o.wakeFreq = Double(value(&i, a)) ?? 19_000
        case "--wake-vol":          o.wakeAmp = Float(value(&i, a)) ?? 0.02
        case "--debounce":          o.debounce = Double(value(&i, a)) ?? 8
        case "--timeout":
            o.timeout = Double(value(&i, a)) ?? 60
            o.timeoutExplicit = true
        case "--log":               o.logPath = value(&i, a)
        default:
            if a.hasPrefix("-psn_") { break }        // LaunchServices leftover
            if a.hasPrefix("-") {
                print(usage)
                finish(.usage, "unknown option \(a)")
            }
            positionals.append(a)
        }
        i += 1
    }

    // The command is the first positional wherever it appears, because the
    // `budsmp` wrapper prepends `--log <path>` to whatever the user typed.
    guard let first = positionals.first else { return o }     // bare `budsmp` => apply
    guard KNOWN_COMMANDS.contains(first) else {
        print(usage)
        finish(.usage, "unknown command \"\(first)\"")
    }
    o.command = first
    o.rawFrames = Array(positionals.dropFirst())
    return o
}

// ---------------------------------------------------------------------------
// MARK: - Commands that do not need a session
// ---------------------------------------------------------------------------

func runScan() -> Never {
    let devices = pairedDevices()
    log("paired devices: \(devices.count)")
    for d in devices {
        log("  \(d.addressString ?? "?")  connected=\(d.isConnected() ? "yes" : "no ")  \"\(d.name ?? "?")\"")
        for r in serviceRecords(d) {
            guard let ch = rfcommChannel(of: r) else { continue }
            let name = r.getServiceName() ?? "(unnamed)"
            let mark = name == SPP4_SERVICE_NAME ? "   <-- MDE_VERSION channel" : ""
            log("      rfcomm \(String(format: "%2d", Int(ch)))  \(name)\(mark)")
        }
    }
    finish(.ok, "\(devices.count) paired device(s)")
}

final class SdpDumper: NSObject {
    func run(_ dev: IOBluetoothDevice) {
        log("performing SDP query on \(dev.addressString ?? "?") ...")
        dev.performSDPQuery(self, uuids: [])
    }

    @objc func sdpQueryComplete(_ device: IOBluetoothDevice!, status: IOReturn) {
        log("SDP query complete status=\(ioHex(status))")
        let records = serviceRecords(device)
        log("service records: \(records.count)")
        for r in records {
            let ch = rfcommChannel(of: r).map { String(format: "%2d", Int($0)) } ?? " -"
            var uuids = ""
            if let attr = r.getAttributeDataElement(0x0001), let arr = attr.getArrayValue() {
                for case let e as IOBluetoothSDPDataElement in arr {
                    if let u = e.getUUIDValue() { uuids += hex(Data(referencing: u)) + " " }
                }
            }
            log("  rfcomm \(ch)  \"\(r.getServiceName() ?? "(unnamed)")\"  [\(uuids.trimmingCharacters(in: .whitespaces))]")
        }
        finish(.ok, "\(records.count) service record(s)")
    }
}

/// Print what the buds report about their stored record, and verify the write.
func reportState(_ rx: [UInt8], expecting asVer: UInt8?, wrote: Bool = true) -> Never {
    let (code, msg) = describeState(rx, expecting: asVer, wrote: wrote)
    finish(code, msg)
}

/// The body of `reportState`, minus the exit — the daemon has to keep running.
func describeState(_ rx: [UInt8], expecting asVer: UInt8?, wrote: Bool) -> (Exit, String) {
    let state = decodeState(rx)
    log("")
    log("--- device state as reported by the buds --------------------------")
    if state.frameCount == 0 {
        log("  no 02 05 4c 0b state frame arrived.")
        if wrote {
            log("  the write may still have landed — re-run `budsmp read` with the buds")
            log("  awake, or start/stop playback to force the record to be re-evaluated.")
        } else {
            log("  the buds only push their state when something makes them re-evaluate")
            log("  the record. Re-run and start or stop playback while it listens.")
        }
        log("------------------------------------------------------------------")
        if asVer != nil { return (.notVerified, "no state frame to verify against") }
        return (wrote ? .ok : .notVerified, "no state frame")
    }
    if !wrote { log("  (nothing was written — this is the stored value)") }
    log("  state frames       : \(state.frameCount)")
    if let v = state.asVer {
        log("  asVer (this host)  : \(v)   \((v == 2 || v == 3) ? "[multipoint allowed]" : "[multipoint blocked]")")
    }
    if state.asVerChanged {
        // The frames the buds send around a write trace the record before and
        // after it, so the first value is what was actually stored beforehand.
        let seq = state.asVerSeen.map(String.init).joined(separator: " -> ")
        log("  asVer as reported  : \(seq)")
        log("                       (first value is what was stored before the write)")
    }
    if let a = state.declaredAccount {
        log(String(format: "  account declared   : 0x%04x%@", a, a == 0 ? "   [none — expected, and fine]" : ""))
    }
    if !state.peerAccounts.isEmpty {
        log("  account of peer(s) : " + state.peerAccounts.map { String(format: "0x%04x", $0) }.joined(separator: ", "))
    }
    log("------------------------------------------------------------------")

    guard let want = asVer else { return (.ok, "") }
    guard let got = state.asVer else { return (.notVerified, "asVer not reported") }
    // The firmware normalises a written 0 up to 1, so only check the pass set.
    if want == got || (want == 0 && got <= 1) { return (.ok, "asVer=\(got)") }
    return (.notVerified, "wrote asVer=\(want) but the device reports \(got)")
}

// ---------------------------------------------------------------------------
// MARK: - Entry point
// ---------------------------------------------------------------------------

let rawArgs = Array(CommandLine.arguments.dropFirst())

// Open the log before parsing, so that usage errors are visible too — the
// wrapper has nothing but this file to report back to the terminal.
let defaultLogPath = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Logs/galaxy-buds-multipoint.log").path
if let idx = rawArgs.firstIndex(of: "--log"), idx + 1 < rawArgs.count {
    LOG = Logger(path: rawArgs[idx + 1])
} else {
    LOG = Logger(path: defaultLogPath)
}

let opts = parseOptions(rawArgs)
// For the daemon the log is a rolling record rather than the output of one
// command, so every line — including this restart marker — needs a time on it.
LOG.stamped = opts.command == "daemon"
log("=== budsmp \(opts.command) ===")

switch opts.command {
case "help":
    print(usage)
    exit(0)

case "frame":
    // Pure computation — no Bluetooth, no permission prompt.
    let f = opts.account.map { mdeWithAccount(version: opts.asVer, selector: opts.selector, hash: $0) }
        ?? mdeVersionOnly(opts.asVer)
    log("asVer=\(opts.asVer)" + (opts.account.map { String(format: " account=0x%04x", $0) } ?? " (version-only)"))
    log(hex(f))
    finish(.ok, hex(f))

case "scan":
    runScan()

case "daemon":
    // Deliberately before findDevice: the daemon takes the device from the
    // connect event, so it starts fine with the buds in the case — or not paired
    // yet at all.
    let daemon = Daemon(opts: opts, wake: opts.wakeExplicit ? opts.wake : false)
    DispatchQueue.main.async { daemon.start() }
    RunLoop.main.run()
    finish(.timeout, "run loop ended")

default:
    break
}

guard let device = findDevice(address: opts.address, nameNeedle: opts.nameNeedle) else {
    finish(.noDevice, "target device not found")
}

if opts.command == "sdp" {
    let dumper = SdpDumper()
    DispatchQueue.main.async { dumper.run(device) }
    DispatchQueue.main.asyncAfter(deadline: .now() + opts.timeout) { finish(.timeout, "SDP query timed out") }
    RunLoop.main.run()
}

var framesToSend: [[UInt8]]
var expectedVersion: UInt8?
var defaultListen: Double

switch opts.command {
case "apply":
    framesToSend = [opts.account.map { mdeWithAccount(version: opts.asVer, selector: opts.selector, hash: $0) }
        ?? mdeVersionOnly(opts.asVer)]
    expectedVersion = opts.asVer
    defaultListen = 6.0

case "revert":
    framesToSend = [mdeVersionOnly(0)]
    expectedVersion = 0
    defaultListen = 6.0

case "read":
    // Re-writing the version byte the device already holds is a no-op that still
    // makes the buds re-evaluate the record and push their state NOTIFY. Plain
    // GETs never carry the account, so this nudge is the only way to read it
    // back from the host alone. See docs/protocol.md.
    framesToSend = [mdeVersionOnly(opts.asVer)]
    expectedVersion = nil
    defaultListen = 10.0

case "watch":
    // Deliberately empty: every other command writes before it reports, so the
    // value it prints is one it just set. This one only ever observes.
    framesToSend = []
    expectedVersion = nil
    defaultListen = 45.0

case "send":
    guard !opts.rawFrames.isEmpty else {
        print(usage)
        finish(.usage, "send needs at least one hex frame")
    }
    framesToSend = opts.rawFrames.map {
        guard let b = hexToBytes($0) else { finish(.usage, "not valid hex: \($0)") }
        return b
    }
    expectedVersion = nil
    defaultListen = 10.0

default:
    print(usage)
    finish(.usage, "unknown command \"\(opts.command)\"")
}

let channelID = resolveChannel(device, override: opts.channel)
let listenSeconds = opts.listenSeconds ?? defaultListen
// A long --listen would otherwise be cut short by the default watchdog.
let watchdog = opts.timeoutExplicit ? opts.timeout : max(opts.timeout, listenSeconds + 25)

if opts.wake {
    tone = WakeTone(freq: opts.wakeFreq, amp: opts.wakeAmp)
    tone?.start()
}

var onListening: (() -> Void)?
if opts.command == "watch" {
    log("watch: sending nothing — whatever arrives is what the buds already hold")
    onListening = {
        // The buds push their state when a record is re-evaluated, and that
        // happens on audio-connection changes. Since we refuse to write, ending
        // the wake tone is the nudge — and if it is off, the user has to play or
        // pause something instead.
        guard tone != nil else {
            log("  start or stop playback now to make the buds re-evaluate the record")
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            log("watch: stopping the wake tone — the audio change should trigger a report")
            log("  (if nothing arrives, start or stop playback while this listens)")
            tone?.stop()
        }
    }
}

let session = Session(device: device,
                      channelID: channelID,
                      frames: framesToSend,
                      listenSeconds: listenSeconds,
                      attempts: opts.attempts,
                      retryDelay: opts.retryDelay,
                      onListening: onListening) { rx in
    tone?.stop()
    reportState(rx, expecting: expectedVersion, wrote: !framesToSend.isEmpty)
}

DispatchQueue.main.async { session.start() }
DispatchQueue.main.asyncAfter(deadline: .now() + watchdog) { finish(.timeout, "timed out") }
RunLoop.main.run()
