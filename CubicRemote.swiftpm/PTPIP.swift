import Foundation

// PTP/IP client for the altek Cubic (C01). Port of the Python version that was
// verified on the real camera. It deliberately copies the original app's quirks
// (see altek-cubic-protocol.md): raw ASCII friendly name, event connection
// number hard-coded to 1, DataPhase always 0, INT32 for every property write,
// strings without NUL, InitiateCapture response ignored.

// MARK: - Constants

enum Op {
    static let getDeviceInfo: UInt16 = 0x1001
    static let openSession: UInt16 = 0x1002
    static let closeSession: UInt16 = 0x1003
    static let getStorageIDs: UInt16 = 0x1004
    static let getObjectHandles: UInt16 = 0x1007
    static let getObjectInfo: UInt16 = 0x1008
    static let getThumb: UInt16 = 0x100A
    static let deleteObject: UInt16 = 0x100B
    static let initiateCapture: UInt16 = 0x100E
    static let formatStore: UInt16 = 0x100F
    static let getDevicePropDesc: UInt16 = 0x1014
    static let getDevicePropValue: UInt16 = 0x1015
    static let setDevicePropValue: UInt16 = 0x1016
    static let getPartialObject: UInt16 = 0x101B
    static let s1Lock: UInt16 = 0x90B1
    static let s1Release: UInt16 = 0x90B2
    static let getQVImage: UInt16 = 0x90B3
    static let touchShot: UInt16 = 0x90B5
    static let zoom: UInt16 = 0x90B7
    static let getStorageFree: UInt16 = 0x90D3
    static let resetAutoPowerOff: UInt16 = 0x90D6
    static let getDSCInfo: UInt16 = 0x90D8
    static let setMacro: UInt16 = 0x90D9
    static let getAutoPowerOff: UInt16 = 0x90DA
    static let selfTimerShot: UInt16 = 0x90DC
}

enum Evt {
    static let objectAdded: UInt16 = 0x4002
    static let devicePropChanged: UInt16 = 0x4006
    static let captureComplete: UInt16 = 0x400D
    static let qvComplete: UInt16 = 0xC0A1
    static let dscPowerOff: UInt16 = 0xC0A2
    static let selfTimerShot: UInt16 = 0xC0A4
    static let autoPowerOff: UInt16 = 0xC0B2
    static let cameraError: UInt16 = 0xC0B3
}

enum Prop {
    static let batteryLevel: UInt16 = 0x5001
    static let functionalMode: UInt16 = 0x5002
    static let stillResolution: UInt16 = 0x5003
    static let flashMode: UInt16 = 0x500C
    static let exposureComp: UInt16 = 0x5010
    static let dateTime: UInt16 = 0x5011
    static let sceneMode: UInt16 = 0x5031
    static let softSkin: UInt16 = 0x5034
    static let saveHighQuality: UInt16 = 0x5035
    static let videoSize: UInt16 = 0x5071
    static let soundVolume: UInt16 = 0x5081
    static let touchShot: UInt16 = 0x5091
}

let respOK: UInt16 = 0x2001

func respName(_ code: UInt16) -> String {
    switch code {
    case 0x2001: return "OK"
    case 0x2002: return "GeneralError"
    case 0x2003: return "SessionNotOpen"
    case 0x2005: return "OperationNotSupported"
    case 0x2009: return "InvalidObjectHandle"
    case 0x200A: return "DevicePropNotSupported"
    case 0x2019: return "DeviceBusy"
    case 0x201D: return "InvalidParameter"
    default: return String(format: "0x%04X", code)
    }
}

func opName(_ op: UInt16) -> String { String(format: "0x%04X", op) }

func evtName(_ code: UInt16) -> String {
    switch code {
    case 0x4002: return "OBJECT_ADDED"
    case 0x4006: return "DEVICE_PROP_CHANGED"
    case 0x400D: return "CAPTURE_COMPLETE"
    case 0xC0A1: return "QV_COMPLETE"
    case 0xC0A2: return "DSC_POWER_OFF"
    case 0xC0A3: return "S2"
    case 0xC0A4: return "SELF_TIMER_SHOT"
    case 0xC0B1: return "AC_IN"
    case 0xC0B2: return "AUTO_POWER_OFF"
    case 0xC0B3: return "CAMERA_ERROR"
    default: return String(format: "0x%04X", code)
    }
}

enum PTPError: LocalizedError {
    case response(op: UInt16, code: UInt16)
    case malformed(String)
    case notConnected

    var errorDescription: String? {
        switch self {
        case .response(let op, let code): return "指令 \(opName(op)) 失敗：\(respName(code))"
        case .malformed(let m): return "資料格式錯誤：\(m)"
        case .notConnected: return "尚未連線到相機"
        }
    }
}

// MARK: - Byte helpers (little endian)

struct ByteWriter {
    var bytes: [UInt8] = []
    mutating func u8(_ v: UInt8) { bytes.append(v) }
    mutating func u16(_ v: UInt16) { bytes.append(UInt8(v & 0xFF)); bytes.append(UInt8(v >> 8)) }
    mutating func u32(_ v: UInt32) {
        for i in 0..<4 { bytes.append(UInt8((v >> (8 * UInt32(i))) & 0xFF)) }
    }
    mutating func u64(_ v: UInt64) {
        for i in 0..<8 { bytes.append(UInt8((v >> (8 * UInt64(i))) & 0xFF)) }
    }
    mutating func append(_ b: [UInt8]) { bytes.append(contentsOf: b) }
}

struct ByteReader {
    let b: [UInt8]
    var pos: Int = 0
    init(_ b: [UInt8], at pos: Int = 0) { self.b = b; self.pos = pos }

    var remaining: Int { b.count - pos }

    mutating func u8() throws -> UInt8 {
        guard pos + 1 <= b.count else { throw PTPError.malformed("資料太短") }
        defer { pos += 1 }
        return b[pos]
    }
    mutating func u16() throws -> UInt16 {
        guard pos + 2 <= b.count else { throw PTPError.malformed("資料太短") }
        defer { pos += 2 }
        return UInt16(b[pos]) | (UInt16(b[pos + 1]) << 8)
    }
    mutating func u32() throws -> UInt32 {
        guard pos + 4 <= b.count else { throw PTPError.malformed("資料太短") }
        var v: UInt32 = 0
        for i in 0..<4 { v |= UInt32(b[pos + i]) << (8 * UInt32(i)) }
        pos += 4
        return v
    }
    mutating func u64() throws -> UInt64 {
        guard pos + 8 <= b.count else { throw PTPError.malformed("資料太短") }
        var v: UInt64 = 0
        for i in 0..<8 { v |= UInt64(b[pos + i]) << (8 * UInt64(i)) }
        pos += 8
        return v
    }
    /// PTP string: [count u8][UTF-16LE chars], count includes the NUL.
    mutating func string() throws -> String {
        let n = Int(try u8())
        guard pos + 2 * n <= b.count else { throw PTPError.malformed("字串太短") }
        var units: [UInt16] = []
        for i in 0..<n {
            let u = UInt16(b[pos + 2 * i]) | (UInt16(b[pos + 2 * i + 1]) << 8)
            if u != 0 { units.append(u) }
        }
        pos += 2 * n
        return String(decoding: units, as: UTF16.self)
    }
    /// Reads a value of PTP datatype `dt` as Int64.
    mutating func value(_ dt: UInt16) throws -> Int64 {
        switch dt {
        case 1: return Int64(Int8(bitPattern: try u8()))
        case 2: return Int64(try u8())
        case 3: return Int64(Int16(bitPattern: try u16()))
        case 4: return Int64(try u16())
        case 5: return Int64(Int32(bitPattern: try u32()))
        case 6: return Int64(try u32())
        case 7: return Int64(bitPattern: try u64())
        case 8: return Int64(bitPattern: try u64())
        case 0xFFFF: _ = try string(); return 0
        default: throw PTPError.malformed("不支援的資料型別 \(dt)")
        }
    }
}

// MARK: - Data types

struct PTPResponse {
    let code: UInt16
    let params: [UInt32]
    let data: [UInt8]
    var ok: Bool { code == respOK }
    var param0: UInt32 { params.first ?? 0 }
}

struct PTPEvent {
    let code: UInt16
    let params: [UInt32]
    var description: String { "\(evtName(code)) \(params.map { String(format: "0x%X", $0) })" }
}

struct PropDesc {
    let code: UInt16
    let dataType: UInt16
    let defaultValue: Int64
    let current: Int64
    let enumValues: [Int64]
}

struct ObjectInfo {
    let storage: UInt32
    let format: UInt16
    let size: UInt32
    let width: UInt32
    let height: UInt32
    let filename: String
    let captureDate: String
}

struct SetupInfo {
    var firmware: UInt32 = 0
    var storageIDs: [UInt32] = []
    var battery: Int = 0
    var photosLeft: Int = 0
    var videoSecondsLeft: Int = 0
    var autoPowerOff: Int = 0
    var hasSD: Bool { storageIDs.contains(0x0001_0001) }
}

// MARK: - Camera

final class CubicCamera {
    static let defaultHost = "192.168.0.1"
    static let ptpPort: UInt16 = 15740
    static let protocolVersion: UInt32 = 0x0001_3000
    static let appGUID: [UInt8] = Array("cubic".utf8) + [UInt8](repeating: 0xFC, count: 11)

    let host: String
    let port: UInt16
    /// The app sends the phone's Wi-Fi MAC string as raw ASCII here.
    var friendlyName = "02:00:00:00:00:00"
    var log: (String) -> Void = { _ in }
    var onEvent: (PTPEvent) -> Void = { _ in }

    private var cmd: TCPSocket?
    private var evt: TCPSocket?
    private var tid: UInt32 = 0
    private let lock = NSRecursiveLock()
    private let stateLock = NSLock()
    private var _running = false
    private var firstCapture = true

    private(set) var firmware: UInt32 = 0
    private(set) var storageIDs: [UInt32] = []
    private(set) var descs: [UInt16: PropDesc] = [:]

    init(host: String = CubicCamera.defaultHost, port: UInt16 = CubicCamera.ptpPort) {
        self.host = host
        self.port = port
    }

    private var running: Bool {
        get { stateLock.lock(); defer { stateLock.unlock() }; return _running }
        set { stateLock.lock(); _running = newValue; stateLock.unlock() }
    }

    var storage: UInt32 { storageIDs.first ?? 0x0001_0001 }

    // MARK: connection

    static func readPacket(_ s: TCPSocket) throws -> (type: UInt32, payload: [UInt8]) {
        var r = ByteReader(try s.receive(exactly: 4))
        let length = Int(try r.u32())
        if length < 8 || length > 64 * 1024 * 1024 {
            throw PTPError.malformed("封包長度不合理：\(length)")
        }
        let rest = try s.receive(exactly: length - 4)
        var rr = ByteReader(rest)
        let type = try rr.u32()
        return (type, Array(rest[4...]))
    }

    func connect() throws {
        log("連線到 \(host):\(port)")
        var lastError: Error?
        var c: TCPSocket?
        for _ in 0..<3 {
            do { c = try TCPSocket(host: host, port: port, timeout: 10); break }
            catch { lastError = error; Thread.sleep(forTimeInterval: 0.5) }
        }
        guard let cmdSock = c else { throw lastError ?? PTPError.notConnected }
        cmdSock.setReceiveTimeout(10)

        // InitCommandRequest
        var body = ByteWriter()
        body.append(CubicCamera.appGUID)
        body.append(Array(friendlyName.utf8))
        body.u32(CubicCamera.protocolVersion)
        var pkt = ByteWriter()
        pkt.u32(UInt32(8 + body.bytes.count))
        pkt.u32(1)
        pkt.append(body.bytes)
        try cmdSock.send(pkt.bytes)
        let ack = try CubicCamera.readPacket(cmdSock)
        guard ack.type == 2 else { cmdSock.close(); throw PTPError.malformed("相機拒絕連線（封包類型 \(ack.type)）") }
        log("InitCommandAck OK")

        // Event channel; the app hard-codes connection number 1.
        let evtSock = try TCPSocket(host: host, port: port, timeout: 10)
        evtSock.setReceiveTimeout(10)
        var ev = ByteWriter()
        ev.u32(12); ev.u32(3); ev.u32(1)
        try evtSock.send(ev.bytes)
        let eack = try CubicCamera.readPacket(evtSock)
        guard eack.type == 4 else { cmdSock.close(); evtSock.close(); throw PTPError.malformed("事件通道被拒（封包類型 \(eack.type)）") }
        evtSock.setReceiveTimeout(1)

        cmd = cmdSock
        evt = evtSock
        tid = 0
        firstCapture = true
        running = true

        let et = Thread { [weak self] in self?.eventLoop() }
        et.name = "cubic-events"
        et.start()

        _ = try transaction(Op.openSession, [1])
        log("Session 已開啟")

        let kt = Thread { [weak self] in self?.keepaliveLoop() }
        kt.name = "cubic-keepalive"
        kt.start()
    }

    /// Same initialisation sequence as the original app.
    func setup() throws -> SetupInfo {
        var info = SetupInfo()
        try setProp(Prop.functionalMode, 0x8002)
        firmware = try transaction(Op.getDSCInfo).param0
        info.firmware = firmware
        storageIDs = try getStorageIDs()
        info.storageIDs = storageIDs
        info.battery = Int(try getProp(Prop.batteryLevel))
        for p in [Prop.flashMode, Prop.videoSize, Prop.touchShot, Prop.sceneMode] {
            do { _ = try getPropDesc(p) } catch { log("讀取屬性 \(String(format: "0x%04X", p)) 失敗：\(error.localizedDescription)") }
        }
        info.photosLeft = Int(try photosRemaining())
        info.videoSecondsLeft = Int(try videoSecondsRemaining())
        if firmware > 8000 {
            info.autoPowerOff = Int(try transaction(Op.getAutoPowerOff).param0)
            _ = try transaction(Op.setMacro, [0])
        }
        return info
    }

    func close() {
        if running, cmd != nil {
            _ = try? transaction(Op.closeSession, check: false)
        }
        running = false
        evt?.close()
        cmd?.close()
        evt = nil
        cmd = nil
    }

    var isConnected: Bool { running && cmd != nil }

    private func eventLoop() {
        while running {
            guard let s = evt else { break }
            do {
                let p = try CubicCamera.readPacket(s)
                guard p.type == 8 else { continue }
                var r = ByteReader(p.payload)
                let code = try r.u16()
                _ = try r.u32()             // transaction id
                var params: [UInt32] = []
                while r.remaining >= 4 { params.append(try r.u32()) }
                let e = PTPEvent(code: code, params: params)
                log("事件：\(e.description)")
                onEvent(e)
            } catch SockError.timeout {
                continue
            } catch {
                if running { log("事件通道中斷：\(error.localizedDescription)") }
                break
            }
        }
    }

    private func keepaliveLoop() {
        // The app sends a PTP/IP Probe on the command socket once per second.
        while running {
            Thread.sleep(forTimeInterval: 1.0)
            if !running { break }
            if lock.try() {
                defer { lock.unlock() }
                do {
                    guard let s = cmd else { break }
                    var w = ByteWriter(); w.u32(8); w.u32(13)
                    try s.send(w.bytes)
                    _ = try CubicCamera.readPacket(s)
                } catch {
                    log("Probe 失敗：\(error.localizedDescription)")
                }
            }
        }
    }

    // MARK: transaction

    @discardableResult
    func transaction(_ op: UInt16, _ params: [UInt32] = [], dataOut: [UInt8]? = nil,
                     check: Bool = true, onData: (([UInt8]) -> Void)? = nil) throws -> PTPResponse {
        lock.lock()
        defer { lock.unlock() }
        guard let s = cmd else { throw PTPError.notConnected }
        let t = tid
        tid &+= 1

        var w = ByteWriter()
        w.u32(UInt32(18 + 4 * params.count))
        w.u32(6)          // Operation Request
        w.u32(0)          // DataPhaseInfo: always 0, like the app
        w.u16(op)
        w.u32(t)
        for p in params { w.u32(p) }
        try s.send(w.bytes)

        if let out = dataOut {
            var sd = ByteWriter()
            sd.u32(20); sd.u32(9); sd.u32(t); sd.u64(UInt64(out.count))
            try s.send(sd.bytes)
            var ed = ByteWriter()
            ed.u32(UInt32(12 + out.count)); ed.u32(12); ed.u32(t); ed.append(out)
            try s.send(ed.bytes)
        }

        var data: [UInt8] = []
        while true {
            let p = try CubicCamera.readPacket(s)
            switch p.type {
            case 9, 14:
                continue    // Start Data / stray Probe response
            case 10, 12:
                let chunk = p.payload.count > 4 ? Array(p.payload[4...]) : []
                if let cb = onData { cb(chunk) } else { data.append(contentsOf: chunk) }
            case 7:
                var r = ByteReader(p.payload)
                let code = try r.u16()
                _ = try r.u32()
                var rp: [UInt32] = []
                while r.remaining >= 4 { rp.append(try r.u32()) }
                let resp = PTPResponse(code: code, params: rp, data: data)
                if check && !resp.ok { throw PTPError.response(op: op, code: code) }
                return resp
            default:
                throw PTPError.malformed("指令 \(opName(op)) 收到未預期的封包類型 \(p.type)")
            }
        }
    }

    // MARK: properties

    @discardableResult
    func getPropDesc(_ prop: UInt16) throws -> PropDesc {
        let d = try transaction(Op.getDevicePropDesc, [UInt32(prop)]).data
        var r = ByteReader(d)
        let code = try r.u16()
        let dt = try r.u16()
        _ = try r.u8()                      // get/set
        let def = try r.value(dt)
        let cur = try r.value(dt)
        let form = try r.u8()
        var values: [Int64] = []
        if form == 1 {
            _ = try r.value(dt); _ = try r.value(dt); _ = try r.value(dt)
        } else if form == 2 {
            let n = Int(try r.u16())
            for _ in 0..<n { values.append(try r.value(dt)) }
        }
        let desc = PropDesc(code: code, dataType: dt, defaultValue: def, current: cur, enumValues: values)
        descs[prop] = desc
        return desc
    }

    func getProp(_ prop: UInt16) throws -> Int64 {
        let d = try transaction(Op.getDevicePropValue, [UInt32(prop)]).data
        var r = ByteReader(d)
        switch d.count {
        case 1: return Int64(try r.u8())
        case 2: return Int64(try r.u16())
        case 4: return Int64(Int32(bitPattern: try r.u32()))
        case 8: return Int64(bitPattern: try r.u64())
        default: throw PTPError.malformed("屬性值長度 \(d.count)")
        }
    }

    /// The app writes every numeric property as a 4-byte INT32.
    func setProp(_ prop: UInt16, _ value: Int64) throws {
        var w = ByteWriter()
        w.u32(UInt32(bitPattern: Int32(truncatingIfNeeded: value)))
        try transaction(Op.setDevicePropValue, [UInt32(prop)], dataOut: w.bytes)
    }

    /// App string format: [num chars][UTF-16LE], no NUL terminator.
    func setPropString(_ prop: UInt16, _ text: String) throws {
        let units = Array(text.utf16)
        var w = ByteWriter()
        w.u8(UInt8(units.count))
        for u in units { w.u16(u) }
        try transaction(Op.setDevicePropValue, [UInt32(prop)], dataOut: w.bytes)
    }

    func syncClock() throws {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd'T'HHmmss"
        try setPropString(Prop.dateTime, f.string(from: Date()))
    }

    func enumValue(_ prop: UInt16, index: Int) throws -> Int64 {
        let d = try descs[prop] ?? getPropDesc(prop)
        guard index >= 0 && index < d.enumValues.count else {
            throw PTPError.malformed("屬性 \(String(format: "0x%04X", prop)) 沒有第 \(index) 個選項")
        }
        return d.enumValues[index]
    }

    // MARK: status

    func getStorageIDs() throws -> [UInt32] {
        var r = ByteReader(try transaction(Op.getStorageIDs).data)
        let n = Int(try r.u32())
        var ids: [UInt32] = []
        for _ in 0..<n { ids.append(try r.u32()) }
        return ids
    }

    func battery() throws -> Int { Int(try getProp(Prop.batteryLevel)) }
    func photosRemaining() throws -> UInt32 { try transaction(Op.getStorageFree, [1]).param0 }
    func videoSecondsRemaining() throws -> UInt32 { try transaction(Op.getStorageFree, [2]).param0 }

    // MARK: shooting

    /// Shutter like the app's button: S1 lock (AF) -> InitiateCapture -> S1 release.
    /// The InitiateCapture response is not treated as an error (the camera answers
    /// DeviceBusy when a recording is being stopped), and S1 release is always sent.
    @discardableResult
    func capture(beep: Bool = true) throws -> UInt16 {
        if firstCapture {
            firstCapture = false
            do { try syncClock() } catch { log("同步時間失敗：\(error.localizedDescription)") }
        }
        try transaction(Op.s1Lock, [beep ? 1 : 0])
        var code: UInt16 = respOK
        do {
            code = try transaction(Op.initiateCapture, [firmware > 10000 ? 2 : 4], check: false).code
        } catch {
            _ = try? transaction(Op.s1Release, check: false)
            throw error
        }
        try transaction(Op.s1Release, check: false)
        return code
    }

    /// In video mode the same shutter sequence starts and stops recording.
    @discardableResult
    func recordToggle(beep: Bool = true) throws -> UInt16 { try capture(beep: beep) }

    func touchShot(x: Int, y: Int) throws {
        try transaction(Op.touchShot, [UInt32(max(0, x)), UInt32(max(0, y))])
    }

    func zoom(tele: Bool, press: Bool) throws {
        try transaction(Op.zoom, [tele ? 1 : 0, press ? 1 : 0])
    }

    func setMode(video: Bool) throws {
        try setProp(Prop.sceneMode, try enumValue(Prop.sceneMode, index: video ? 1 : 0))
    }

    func quickView() throws -> [UInt8] { try transaction(Op.getQVImage).data }
    func setSelfTimer(_ seconds: Int) throws { try transaction(Op.selfTimerShot, [UInt32(seconds)]) }
    func setMacro(_ on: Bool) throws { try transaction(Op.setMacro, [on ? 1 : 0]) }
    func setAutoPowerOff(_ seconds: Int) throws { try transaction(Op.resetAutoPowerOff, [UInt32(seconds)]) }

    // MARK: files

    func listHandles() throws -> [UInt32] {
        var r = ByteReader(try transaction(Op.getObjectHandles, [storage, 0]).data)
        let n = Int(try r.u32())
        var hs: [UInt32] = []
        for _ in 0..<n { hs.append(try r.u32()) }
        return hs
    }

    func objectInfo(_ handle: UInt32) throws -> ObjectInfo {
        var r = ByteReader(try transaction(Op.getObjectInfo, [handle]).data)
        let storage = try r.u32()
        let format = try r.u16()
        _ = try r.u16()                    // protection
        let size = try r.u32()
        _ = try r.u16()                    // thumb format
        _ = try r.u32(); _ = try r.u32(); _ = try r.u32()  // thumb size, w, h
        let w = try r.u32()
        let h = try r.u32()
        _ = try r.u32(); _ = try r.u32()  // bit depth, parent
        _ = try r.u16()                    // association type
        _ = try r.u32(); _ = try r.u32()  // association desc, sequence
        let name = try r.string()
        let date = (try? r.string()) ?? ""
        return ObjectInfo(storage: storage, format: format, size: size, width: w, height: h,
                          filename: name, captureDate: date)
    }

    func thumbnail(_ handle: UInt32) throws -> [UInt8] { try transaction(Op.getThumb, [handle]).data }

    /// Downloads with GetPartialObject in 1843200-byte chunks, like the app.
    func download(_ handle: UInt32, to url: URL, progress: ((Double) -> Void)? = nil) throws {
        let size = try objectInfo(handle).size
        FileManager.default.createFile(atPath: url.path, contents: nil)
        let fh = try FileHandle(forWritingTo: url)
        defer { try? fh.close() }
        var offset: UInt32 = 0
        let chunk: UInt32 = 1_843_200
        while offset < size {
            let want = min(chunk, size - offset)
            var got: UInt32 = 0
            try transaction(Op.getPartialObject, [handle, offset, want], onData: { bytes in
                fh.write(Data(bytes))
                got += UInt32(bytes.count)
            })
            if got == 0 { break }
            offset += got
            progress?(Double(offset) / Double(max(size, 1)))
        }
    }

    func delete(_ handle: UInt32) throws { try transaction(Op.deleteObject, [handle]) }
    func formatCard() throws { try transaction(Op.formatStore, [storage, 0]) }
}
