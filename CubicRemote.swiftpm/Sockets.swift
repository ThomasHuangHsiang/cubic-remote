import Foundation
import Darwin

/// Minimal blocking TCP / UDP sockets (BSD API). Every call here is meant to run
/// on a background thread, never on the main thread.

enum SockError: LocalizedError {
    case failed(String)
    case closed
    case timeout

    var errorDescription: String? {
        switch self {
        case .failed(let m): return m
        case .closed: return "連線被相機關閉"
        case .timeout: return "等待相機回應逾時"
        }
    }
}

private func makeAddress(_ host: String, _ port: UInt16) throws -> sockaddr_in {
    var addr = sockaddr_in()
    addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    if inet_pton(AF_INET, host, &addr.sin_addr) != 1 {
        throw SockError.failed("IP 位址格式錯誤：\(host)")
    }
    return addr
}

private func setIntOption(_ fd: Int32, _ level: Int32, _ name: Int32, _ value: Int32) {
    var v = value
    _ = setsockopt(fd, level, name, &v, socklen_t(MemoryLayout<Int32>.size))
}

private func setTimeoutOption(_ fd: Int32, _ name: Int32, _ seconds: Double) {
    let whole = Int(seconds)
    var tv = timeval()
    tv.tv_sec = whole
    tv.tv_usec = Int32((seconds - Double(whole)) * 1_000_000)
    _ = setsockopt(fd, SOL_SOCKET, name, &tv, socklen_t(MemoryLayout<timeval>.size))
}

final class TCPSocket {
    private var fd: Int32 = -1
    private let fdLock = NSLock()

    init(host: String, port: UInt16, timeout: Double = 10) throws {
        let s = socket(AF_INET, SOCK_STREAM, 0)
        if s < 0 { throw SockError.failed("無法建立 socket（errno \(errno)）") }
        var addr = try makeAddress(host, port)

        // non-blocking connect so we can apply a timeout
        let flags = fcntl(s, F_GETFL, 0)
        _ = fcntl(s, F_SETFL, flags | O_NONBLOCK)
        let rc = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                Darwin.connect(s, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if rc != 0 {
            if errno != EINPROGRESS {
                let e = errno
                Darwin.close(s)
                throw SockError.failed("無法連線到 \(host):\(port)（errno \(e)）")
            }
            var pfd = pollfd(fd: s, events: Int16(POLLOUT), revents: 0)
            let pr = poll(&pfd, 1, Int32(timeout * 1000))
            if pr <= 0 {
                Darwin.close(s)
                throw SockError.failed("連線到 \(host):\(port) 逾時")
            }
            var soErr: Int32 = 0
            var len = socklen_t(MemoryLayout<Int32>.size)
            _ = getsockopt(s, SOL_SOCKET, SO_ERROR, &soErr, &len)
            if soErr != 0 {
                Darwin.close(s)
                throw SockError.failed("無法連線到 \(host):\(port)（errno \(soErr)）")
            }
        }
        _ = fcntl(s, F_SETFL, flags)   // back to blocking
        setIntOption(s, SOL_SOCKET, SO_NOSIGPIPE, 1)
        setIntOption(s, Int32(IPPROTO_TCP), TCP_NODELAY, 1)
        fd = s
    }

    deinit { close() }

    func setReceiveTimeout(_ seconds: Double) {
        setTimeoutOption(fd, SO_RCVTIMEO, seconds)
    }

    func send(_ bytes: [UInt8]) throws {
        if bytes.isEmpty { return }
        var off = 0
        while off < bytes.count {
            let n = bytes.withUnsafeBytes { raw -> Int in
                Darwin.send(fd, raw.baseAddress! + off, raw.count - off, 0)
            }
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { throw SockError.closed }
            off += n
        }
    }

    /// Reads exactly `count` bytes. Throws `.timeout` only if nothing at all arrived.
    func receive(exactly count: Int) throws -> [UInt8] {
        var buf = [UInt8](repeating: 0, count: count)
        var got = 0
        while got < count {
            let n = buf.withUnsafeMutableBytes { raw -> Int in
                Darwin.recv(fd, raw.baseAddress! + got, count - got, 0)
            }
            if n > 0 { got += n; continue }
            if n == 0 { throw SockError.closed }
            let e = errno
            if e == EINTR { continue }
            if e == EAGAIN || e == EWOULDBLOCK {
                if got == 0 { throw SockError.timeout }
                continue            // keep waiting for the rest of a packet
            }
            throw SockError.closed
        }
        return buf
    }

    /// One recv() call, up to `max` bytes (used for RTSP text replies).
    func receive(upTo max: Int) throws -> [UInt8] {
        var buf = [UInt8](repeating: 0, count: max)
        let n = buf.withUnsafeMutableBytes { raw -> Int in
            Darwin.recv(fd, raw.baseAddress!, max, 0)
        }
        if n == 0 { throw SockError.closed }
        if n < 0 {
            if errno == EAGAIN || errno == EWOULDBLOCK { throw SockError.timeout }
            throw SockError.closed
        }
        return Array(buf[0..<n])
    }

    func close() {
        fdLock.lock()
        defer { fdLock.unlock() }
        if fd >= 0 {
            _ = Darwin.shutdown(fd, SHUT_RDWR)
            _ = Darwin.close(fd)
            fd = -1
        }
    }
}

final class UDPSocket {
    private var fd: Int32 = -1
    private let fdLock = NSLock()

    init(port: UInt16) throws {
        let s = socket(AF_INET, SOCK_DGRAM, 0)
        if s < 0 { throw SockError.failed("無法建立 UDP socket（errno \(errno)）") }
        setIntOption(s, SOL_SOCKET, SO_REUSEADDR, 1)
        setIntOption(s, SOL_SOCKET, SO_RCVBUF, 4 * 1024 * 1024)
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr = in_addr(s_addr: 0)          // INADDR_ANY
        let rc = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                Darwin.bind(s, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if rc != 0 {
            let e = errno
            Darwin.close(s)
            throw SockError.failed(e == EADDRINUSE ? "UDP 埠 \(port) 被佔用（可能是舊版 app 還在背景執行）"
                                                   : "無法使用 UDP 埠 \(port)（errno \(e)）")
        }
        setTimeoutOption(s, SO_RCVTIMEO, 0.5)
        fd = s
    }

    deinit { close() }

    /// Returns the number of bytes received, 0 on timeout, -1 if the socket is closed.
    func receive(into buf: inout [UInt8]) -> Int {
        let current = fd
        if current < 0 { return -1 }
        let cap = buf.count
        let n = buf.withUnsafeMutableBytes { raw -> Int in
            Darwin.recv(current, raw.baseAddress!, cap, 0)
        }
        if n >= 0 { return n }
        let e = errno
        if e == EAGAIN || e == EWOULDBLOCK || e == EINTR { return 0 }
        return -1
    }

    func close() {
        fdLock.lock()
        defer { fdLock.unlock() }
        if fd >= 0 {
            _ = Darwin.close(fd)
            fd = -1
        }
    }
}
