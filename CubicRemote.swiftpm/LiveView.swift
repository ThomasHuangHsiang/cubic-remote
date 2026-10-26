import Foundation

// Live view: RTSP session + RTP/JPEG (RFC 2435), rebuilt exactly like the original
// app: frames are grouped by sequence number, start at fragment offset 0 and end at
// the marker bit. The RTP timestamp is ignored (the camera fills it inconsistently).

enum JPEGTemplate {
    /// Header template copied byte-for-byte from the app (RTP_Server_Class.JPG_HEADER).
    static let header: [UInt8] = hexBytes([
        "ffd8ffdb00840000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
        "0000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000",
        "000000000000000000000000000000000000000000000000000000000000000000000000ffc0001108000000000301220002",
        "1101031101ffc401a20000010501010101010100000000000000000102030405060708090a0b100002010303020403050504",
        "040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25",
        "262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788",
        "898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3",
        "e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9fa0100030101010101010101010000000000000102030405060708090a0b11000201",
        "02040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a",
        "162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a7374757677",
        "78797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3",
        "d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00"
    ].joined())

    static let lumaQPos = 7
    static let chromaQPos = 72
    static let heightPos = 141
    static let widthPos = 143

    static let lumaQ: [UInt8] = [16, 11, 12, 14, 12, 10, 16, 14, 13, 14, 18, 17, 16, 19, 24, 40,
                                 26, 24, 22, 22, 24, 49, 35, 37, 29, 40, 58, 51, 61, 60, 57, 51,
                                 56, 55, 64, 72, 92, 78, 64, 68, 87, 69, 55, 56, 80, 109, 81, 87,
                                 95, 98, 103, 104, 103, 62, 77, 113, 121, 112, 100, 120, 92, 101, 103, 99]
    static let chromaQ: [UInt8] = [17, 18, 18, 24, 21, 24, 47, 26, 26, 47, 99, 66, 56, 66]
        + [UInt8](repeating: 99, count: 50)

    /// RFC 2435 quality factor -> quantisation tables (same arithmetic as the app).
    static func tables(q: Int) -> ([UInt8], [UInt8]) {
        let factor = min(max(q, 1), 99)
        let scale = q < 50 ? 5000 / factor : 200 - factor * 2
        func scaled(_ t: [UInt8]) -> [UInt8] {
            t.map { b -> UInt8 in UInt8(min(max((Int(b) * scale + 50) / 100, 1), 255)) }
        }
        return (scaled(lumaQ), scaled(chromaQ))
    }
}

func hexBytes(_ s: String) -> [UInt8] {
    var out: [UInt8] = []
    out.reserveCapacity(s.count / 2)
    var hi: UInt8? = nil
    for c in s.utf8 {
        let v: UInt8
        switch c {
        case 48...57: v = c - 48          // 0-9
        case 97...102: v = c - 87         // a-f
        case 65...70: v = c - 55          // A-F
        default: continue
        }
        if let h = hi { out.append(h << 4 | v); hi = nil } else { hi = v }
    }
    return out
}

final class FrameAssembler {
    var onFrame: ([UInt8]) -> Void = { _ in }
    private var buf: [UInt8]? = nil
    private var nextSeq: UInt16 = 0
    private(set) var packets = 0
    private(set) var nonJPEG = 0
    private(set) var frames = 0
    private(set) var dropped = 0
    private(set) var firstPacketHex = ""

    func feed(_ pkt: ArraySlice<UInt8>) {
        let p = Array(pkt)
        packets += 1
        if packets == 1 { firstPacketHex = p.prefix(32).map { String(format: "%02x", $0) }.joined() }
        if p.count < 20 { nonJPEG += 1; return }
        let b1 = p[1]
        if (b1 & 0x7F) != 26 { nonJPEG += 1; return }      // payload type 26 = JPEG
        let marker = (b1 & 0x80) != 0
        let seq = UInt16(p[2]) << 8 | UInt16(p[3])
        var pos = 12                                        // the app assumes a fixed 12-byte header
        let off = Int(p[pos + 1]) << 16 | Int(p[pos + 2]) << 8 | Int(p[pos + 3])
        let q = Int(p[pos + 5])
        let w8 = Int(p[pos + 6])
        let h8 = Int(p[pos + 7])
        pos += 8

        if off == 0 {
            if buf != nil { dropped += 1 }
            var lq: [UInt8]
            var cq: [UInt8]
            if q >= 128 {
                // in-band tables: MBZ, precision, length(2), luma(64), chroma(64)
                guard p.count >= pos + 132 else { buf = nil; dropped += 1; return }
                lq = Array(p[(pos + 4)..<(pos + 68)])
                cq = Array(p[(pos + 68)..<(pos + 132)])
                pos += 132
            } else {
                (lq, cq) = JPEGTemplate.tables(q: q)
            }
            var hdr = JPEGTemplate.header
            hdr.replaceSubrange(JPEGTemplate.lumaQPos..<(JPEGTemplate.lumaQPos + 64), with: lq)
            hdr.replaceSubrange(JPEGTemplate.chromaQPos..<(JPEGTemplate.chromaQPos + 64), with: cq)
            let h = h8 * 8, w = w8 * 8
            hdr[JPEGTemplate.heightPos] = UInt8((h >> 8) & 0xFF)
            hdr[JPEGTemplate.heightPos + 1] = UInt8(h & 0xFF)
            hdr[JPEGTemplate.widthPos] = UInt8((w >> 8) & 0xFF)
            hdr[JPEGTemplate.widthPos + 1] = UInt8(w & 0xFF)
            buf = hdr
        } else if buf == nil {
            return                                          // waiting for the start of a frame
        } else if seq != nextSeq {
            dropped += 1                                    // lost a packet: discard this frame
            buf = nil
            return
        }

        guard var frame = buf else { return }
        let data = p[pos...]
        let at = JPEGTemplate.header.count + off
        if at > frame.count {
            frame.append(contentsOf: [UInt8](repeating: 0, count: at - frame.count))
        }
        let end = at + data.count
        if end > frame.count {
            frame.append(contentsOf: [UInt8](repeating: 0, count: end - frame.count))
        }
        frame.replaceSubrange(at..<end, with: data)
        nextSeq = seq &+ 1

        if marker {
            buf = nil
            if !(frame.count >= 2 && frame[frame.count - 2] == 0xFF && frame[frame.count - 1] == 0xD9) {
                frame.append(contentsOf: [0xFF, 0xD9])
            }
            frames += 1
            onFrame(frame)
        } else {
            buf = frame
        }
    }
}

final class LiveView {
    static let rtspPort: UInt16 = 554
    static let rtpPort: UInt16 = 65362
    static let userAgent = "User-Agent: XXXX Company RTSP Client 0.0001"   // as sent by the app

    let host: String
    var log: (String) -> Void = { _ in }
    var onFrame: ([UInt8]) -> Void = { _ in }

    private var rtsp: TCPSocket?
    private var udp: UDPSocket?
    private(set) var port: UInt16 = LiveView.rtpPort
    private var cseq = 0
    private let session = Int32.random(in: 0..<Int32.max)
    private let stateLock = NSLock()
    private var _running = false
    let assembler = FrameAssembler()

    init(host: String) { self.host = host }

    private var running: Bool {
        get { stateLock.lock(); defer { stateLock.unlock() }; return _running }
        set { stateLock.lock(); _running = newValue; stateLock.unlock() }
    }

    private func request(_ method: String, _ headers: [String]) throws {
        let nl = "\n"                                       // the app (Android) used "\n"
        var msg = "\(method) rtsp://\(host)/MJPG RTSP/1.0\(nl)CSeq: \(cseq)\(nl)\(LiveView.userAgent)\(nl)"
        for h in headers { msg += h + nl }
        msg += nl
        guard let s = rtsp else { throw PTPError.notConnected }
        try s.send(Array(msg.utf8))
        let reply = String(decoding: try s.receive(upTo: 4096), as: UTF8.self)
        let first = reply.split(separator: "\n").first.map(String.init) ?? "(沒有回應)"
        log("RTSP \(method) -> \(first.trimmingCharacters(in: .whitespacesAndNewlines))")
        if reply.contains("RTSP/1.0 200 OK") { cseq += 1 }
    }

    func start() throws {
        // Bind UDP first so no packet is lost. The app always uses 65362; if that port is
        // taken (e.g. an older copy of this app still running) fall back to another one
        // and tell the camera through RTSP SETUP's client_port.
        let candidates: [UInt16] = [LiveView.rtpPort] + stride(from: 65364, through: 65400, by: 2).map { UInt16($0) }
        var lastError: Error?
        for p in candidates {
            do {
                udp = try UDPSocket(port: p)
                port = p
                break
            } catch {
                lastError = error
            }
        }
        guard udp != nil else { throw lastError ?? SockError.failed("無法開啟 UDP 埠") }
        if port != LiveView.rtpPort { log("UDP \(LiveView.rtpPort) 被佔用，改用 \(port)") }
        let s = try TCPSocket(host: host, port: LiveView.rtspPort, timeout: 5)
        s.setReceiveTimeout(5)
        rtsp = s
        cseq = 0
        try request("OPTIONS", [])
        try request("DESCRIBE", ["Accept: Applecation/sdp"])   // typo is the app's
        try request("SETUP", ["Transport: RTP/AVP;unicast;client_port=\(port)-\(port + 1)"])
        try request("PLAY", ["Session: \(session)", "Range: npt=0.000-"])

        assembler.onFrame = { [weak self] jpeg in self?.onFrame(jpeg) }
        running = true
        let t = Thread { [weak self] in self?.receiveLoop() }
        t.name = "cubic-rtp"
        t.start()
    }

    private func receiveLoop() {
        var buf = [UInt8](repeating: 0, count: 65536)
        var announced = false
        while running {
            guard let u = udp else { break }
            let n = u.receive(into: &buf)
            if n < 0 { break }
            if n == 0 { continue }
            assembler.feed(buf[0..<n])
            if !announced {
                announced = true
                log("收到第一個 RTP 封包（\(n) bytes）：\(assembler.firstPacketHex)")
            }
        }
    }

    var stats: String {
        "封包 \(assembler.packets)（非 JPEG \(assembler.nonJPEG)），畫面 \(assembler.frames)，丟棄 \(assembler.dropped)"
    }

    deinit { stop() }

    func stop() {
        running = false
        rtsp?.close()
        udp?.close()
        rtsp = nil
        udp = nil
    }
}
