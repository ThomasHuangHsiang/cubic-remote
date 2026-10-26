import SwiftUI
import UIKit

// MARK: - Settings shown in the UI (same values as the original app)

struct SettingDef: Identifiable {
    enum Kind {
        case prop(UInt16)
        case videoSize
        case selfTimer
        case macro
        case autoPowerOff
    }
    let key: String
    let label: String
    let kind: Kind
    let defaultValue: Int
    let options: [(Int, String)]
    var id: String { key }
}

let allSettings: [SettingDef] = [
    SettingDef(key: "ev", label: "亮度", kind: .prop(Prop.exposureComp), defaultValue: 0,
               options: [(2000, "最亮"), (1000, "微亮"), (0, "正常"), (-1000, "微暗"), (-2000, "最暗")]),
    SettingDef(key: "flash", label: "閃光燈", kind: .prop(Prop.flashMode), defaultValue: 1,
               options: [(1, "自動"), (2, "關閉"), (3, "強制")]),
    SettingDef(key: "res", label: "相片解析度", kind: .prop(Prop.stillResolution), defaultValue: 0,
               options: [(0, "13M"), (1, "10M"), (2, "8M")]),
    SettingDef(key: "hq", label: "儲存高解析度相片", kind: .prop(Prop.saveHighQuality), defaultValue: 1,
               options: [(1, "開"), (0, "關")]),
    SettingDef(key: "skin", label: "美肌", kind: .prop(Prop.softSkin), defaultValue: 0,
               options: [(3, "強"), (2, "中"), (1, "弱"), (0, "關")]),
    SettingDef(key: "touch", label: "觸控拍攝", kind: .prop(Prop.touchShot), defaultValue: 2,
               options: [(1, "開"), (2, "關")]),
    SettingDef(key: "video", label: "錄影模式", kind: .videoSize, defaultValue: 0,
               options: [(0, "Full HD"), (1, "YouTube（≤60 秒）"), (2, "Instagram（≤15 秒）")]),
    SettingDef(key: "timer", label: "定時拍照", kind: .selfTimer, defaultValue: 0,
               options: [(0, "關"), (3, "3 秒")]),
    SettingDef(key: "macro", label: "近拍模式", kind: .macro, defaultValue: 0,
               options: [(0, "關"), (1, "開")]),
    SettingDef(key: "sound", label: "相機聲音", kind: .prop(Prop.soundVolume), defaultValue: 3,
               options: [(3, "開"), (0, "關")]),
    SettingDef(key: "apo", label: "自動關機", kind: .autoPowerOff, defaultValue: 300,
               options: [(60, "1 分鐘"), (300, "5 分鐘"), (57600, "關閉")]),
]

struct CameraFile: Identifiable {
    let id: UInt32
    let name: String
    let size: UInt32
    var thumb: UIImage? = nil
    var localURL: URL? = nil
    var progress: Double? = nil

    var isVideo: Bool {
        let n = name.uppercased()
        return n.hasSuffix(".MOV") || n.hasSuffix(".MP4") || n.hasSuffix(".AVI")
    }
    var sizeText: String {
        size >= 1_048_576 ? String(format: "%.1f MB", Double(size) / 1_048_576)
                          : "\(max(1, Int(size) / 1024)) KB"
    }
}

// MARK: - Model

final class CameraModel: ObservableObject {
    enum ConnState { case disconnected, connecting, connected, failed }

    @Published var state: ConnState = .disconnected
    @Published var errorText = ""
    @Published var alertMessage: String? = nil
    @Published var info = SetupInfo()
    @Published var liveImage: UIImage? = nil
    @Published var liveStats = ""
    @Published var videoMode = false
    @Published var recording = false
    @Published var recStart = Date()
    @Published var recLimit = 0
    @Published var values: [String: Int]
    @Published var logs: [String] = []
    @Published var lastShot: UIImage? = nil
    @Published var files: [CameraFile] = []
    @Published var busy = false
    @Published var loadingFiles = false

    var host = CubicCamera.defaultHost

    private var cam: CubicCamera?
    private var live: LiveView?
    private let work = DispatchQueue(label: "cubic.work")       // all camera calls, in order
    private let frameLock = NSLock()
    private var frameInFlight = false
    private var timer: Timer?
    private var tick = 0
    /// Handles reported by OBJECT_ADDED during this session. The camera's own file
    /// list (GetObjectHandles) does not update until reconnect, so like the original
    /// app we remember new files from events and merge them in.
    private var addedHandles: [UInt32] = []

    init() {
        var v: [String: Int] = [:]
        for s in allSettings { v[s.key] = s.defaultValue }
        values = v
    }

    var isConnected: Bool { state == .connected }

    var canSaveToPhotos: Bool {
        Bundle.main.object(forInfoDictionaryKey: "NSPhotoLibraryAddUsageDescription") != nil
    }

    static var documents: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    // MARK: helpers

    private func ui(_ block: @escaping () -> Void) {
        DispatchQueue.main.async(execute: block)
    }

    func log(_ s: String) {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        let line = "\(f.string(from: Date())) \(s)"
        print(line)
        ui {
            self.logs.append(line)
            if self.logs.count > 500 { self.logs.removeFirst(self.logs.count - 500) }
        }
    }

    private func fail(_ what: String, _ error: Error) {
        log("\(what)失敗：\(error.localizedDescription)")
        ui { self.alertMessage = "\(what)失敗：\(error.localizedDescription)" }
    }

    /// Runs `body` on the camera queue. Call from the main thread.
    private func run(_ what: String, showBusy: Bool = true, _ body: @escaping (CubicCamera) throws -> Void) {
        guard let c = cam, state == .connected else {
            alertMessage = "尚未連線到相機"
            return
        }
        if showBusy { busy = true }
        work.async {
            do { try body(c) } catch { self.fail(what, error) }
            if showBusy { self.ui { self.busy = false } }
        }
    }

    // MARK: connection

    func connect() {
        if state == .connecting { return }
        let oldCam = cam, oldLive = live
        cam = nil
        live = nil
        state = .connecting
        errorText = ""
        liveImage = nil
        let host = self.host
        work.async {
            oldLive?.stop()
            oldCam?.close()
            let c = CubicCamera(host: host)
            c.log = { [weak self] m in self?.log("  " + m) }
            c.onEvent = { [weak self] e in self?.handle(e) }
            do {
                try c.connect()
                let info = try c.setup()
                do { try c.syncClock() } catch { self.log("同步時間失敗：\(error.localizedDescription)") }

                // read back current settings
                var vals: [String: Int] = [:]
                for s in allSettings {
                    switch s.kind {
                    case .prop(let code):
                        if let d = try? c.getPropDesc(code) { vals[s.key] = Int(d.current) }
                    case .videoSize:
                        if let d = try? c.getPropDesc(Prop.videoSize), let i = d.enumValues.firstIndex(of: d.current) {
                            vals[s.key] = i
                        }
                    default:
                        break
                    }
                }
                if info.autoPowerOff > 0 { vals["apo"] = info.autoPowerOff }
                var video = false
                if let d = try? c.getPropDesc(Prop.sceneMode), d.enumValues.count > 1 {
                    video = d.current == d.enumValues[1]
                }

                let lv = LiveView(host: host)
                lv.log = { [weak self] m in self?.log("  " + m) }
                lv.onFrame = { [weak self] jpeg in self?.deliver(jpeg) }
                do {
                    try lv.start()
                    self.log("即時預覽已啟動")
                } catch {
                    self.log("即時預覽啟動失敗：\(error.localizedDescription)")
                }
                self.log("已連線：韌體 \(info.firmware)、電量 \(info.battery)%、可拍 \(info.photosLeft) 張")
                self.ui {
                    self.cam = c
                    self.live = lv
                    self.info = info
                    for (k, v) in vals { self.values[k] = v }
                    self.videoMode = video
                    self.recording = false
                    self.addedHandles = []
                    self.state = .connected
                    self.startTimer()
                }
            } catch {
                c.close()
                self.log("連線失敗：\(error.localizedDescription)")
                self.ui {
                    self.state = .failed
                    self.errorText = error.localizedDescription
                }
            }
        }
    }

    /// Restarts only the RTSP/RTP live view, keeping the PTP session.
    func restartLive() {
        guard isConnected else { return }
        let old = live
        live = nil
        liveImage = nil
        let host = self.host
        work.async {
            old?.stop()
            Thread.sleep(forTimeInterval: 0.3)
            let lv = LiveView(host: host)
            lv.log = { [weak self] m in self?.log("  " + m) }
            lv.onFrame = { [weak self] jpeg in self?.deliver(jpeg) }
            do {
                try lv.start()
                self.log("即時預覽已重新啟動")
            } catch {
                self.log("即時預覽啟動失敗：\(error.localizedDescription)")
            }
            self.ui { self.live = lv }
        }
    }

    func disconnect() {
        let c = cam, l = live
        cam = nil
        live = nil
        timer?.invalidate()
        timer = nil
        state = .disconnected
        recording = false
        liveImage = nil
        work.async {
            l?.stop()
            c?.close()
        }
        log("已中斷連線")
    }

    private func startTimer() {
        timer?.invalidate()
        tick = 0
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.onTimer()
        }
    }

    private func onTimer() {
        tick += 1
        if let l = live { liveStats = l.stats }
        // stop at the same limits as the app
        if recording && recLimit > 0 && Date().timeIntervalSince(recStart) >= Double(recLimit) {
            log("已達錄影時間上限，自動停止")
            toggleRecord()
        }
        if tick % 30 == 0 && !recording && !busy {
            refresh(quiet: true)
        }
        if let c = cam, !c.isConnected, state == .connected {
            state = .failed
            errorText = "與相機的連線中斷了"
        }
    }

    // MARK: live frames

    private func deliver(_ jpeg: [UInt8]) {
        frameLock.lock()
        if frameInFlight { frameLock.unlock(); return }      // drop frames while the UI is busy
        frameInFlight = true
        frameLock.unlock()
        let img = UIImage(data: Data(jpeg))
        let ready = img?.preparingForDisplay() ?? img
        ui {
            if let r = ready { self.liveImage = r }
            self.frameLock.lock()
            self.frameInFlight = false
            self.frameLock.unlock()
        }
    }

    // MARK: events (called on the event thread)

    private func handle(_ e: PTPEvent) {
        switch e.code {
        case Evt.qvComplete:
            work.async { self.fetchQuickView() }
        case Evt.objectAdded:
            let h = e.params.first
            ui {
                if let h = h, !self.addedHandles.contains(h) { self.addedHandles.append(h) }
                if self.recording && self.videoMode && Date().timeIntervalSince(self.recStart) > 2 {
                    self.recording = false
                    self.log("相機已停止錄影並存檔")
                }
            }
        case Evt.selfTimerShot:
            log("自拍倒數中…")
        case Evt.autoPowerOff, Evt.dscPowerOff:
            log("相機即將關機")
        default:
            break
        }
    }

    private func fetchQuickView() {
        guard let c = cam else { return }
        do {
            let jpg = try c.quickView()
            if !jpg.isEmpty {
                let data = Data(jpg)
                let f = DateFormatter()
                f.dateFormat = "yyyyMMdd_HHmmss"
                let url = CameraModel.documents.appendingPathComponent("cubic_\(f.string(from: Date())).jpg")
                try? data.write(to: url)
                let img = UIImage(data: data)
                ui { self.lastShot = img }
                log("預覽圖已存到 App 的文件資料夾")
            }
            let left = Int(try c.photosRemaining())
            ui { self.info.photosLeft = left }
        } catch {
            log("取得預覽圖失敗：\(error.localizedDescription)")
        }
    }

    // MARK: actions (call from the main thread)

    func refresh(quiet: Bool = false) {
        run("更新狀態", showBusy: !quiet) { c in
            let b = try c.battery()
            let p = Int(try c.photosRemaining())
            let v = Int(try c.videoSecondsRemaining())
            self.ui {
                self.info.battery = b
                self.info.photosLeft = p
                self.info.videoSecondsLeft = v
            }
        }
    }

    func shutter() {
        if busy { return }
        if videoMode { toggleRecord(); return }
        let beep = (values["sound"] ?? 3) != 0
        run("拍照") { c in
            let code = try c.capture(beep: beep)
            self.log(code == respOK ? "快門" : "快門（相機回應 \(respName(code))）")
        }
    }

    func toggleRecord() {
        let wasRecording = recording
        let inVideo = videoMode
        let sizeIndex = values["video"] ?? 0
        let beep = (values["sound"] ?? 3) != 0
        run("錄影") { c in
            if !inVideo {
                try c.setMode(video: true)
                self.ui { self.videoMode = true }
            }
            if !wasRecording {
                let left = Int(try c.videoSecondsRemaining())
                let cap: Int? = sizeIndex == 1 ? 61 : (sizeIndex == 2 ? 16 : nil)
                let limit = cap.map { min($0, left) } ?? left
                if limit <= 0 { throw PTPError.malformed("記憶空間不足，無法錄影") }
                let code = try c.recordToggle(beep: beep)
                if code != respOK { self.log("（開始錄影時相機回應 \(respName(code))）") }
                self.ui {
                    self.recording = true
                    self.recStart = Date()
                    self.recLimit = limit
                }
                self.log("開始錄影（最長 \(limit) 秒）")
            } else {
                let code = try c.recordToggle(beep: beep)
                if code != respOK {
                    // the camera answers DeviceBusy while it is already stopping; the app ignores it too
                    self.log("（停止時相機回應 \(respName(code))，屬正常現象）")
                }
                self.ui { self.recording = false }
                self.log("停止錄影，相機存檔中…")
            }
        }
    }

    func setMode(video: Bool) {
        if recording { alertMessage = "錄影中不能切換模式"; return }
        if video == videoMode { return }
        run("切換模式") { c in
            try c.setMode(video: video)
            self.ui { self.videoMode = video }
            self.log(video ? "切換到錄影模式" : "切換到拍照模式")
        }
    }

    func zoom(tele: Bool, press: Bool) {
        run("變焦", showBusy: false) { c in try c.zoom(tele: tele, press: press) }
    }

    func touchShot(x: Int, y: Int) {
        run("觸控拍攝") { c in
            try c.touchShot(x: x, y: y)
            self.log("觸控拍攝 (\(x), \(y))")
        }
    }

    func set(_ s: SettingDef, _ value: Int) {
        let old = values[s.key]
        values[s.key] = value
        run(s.label, showBusy: false) { c in
            do {
                switch s.kind {
                case .prop(let code): try c.setProp(code, Int64(value))
                case .videoSize: try c.setProp(Prop.videoSize, try c.enumValue(Prop.videoSize, index: value))
                case .selfTimer: try c.setSelfTimer(value)
                case .macro: try c.setMacro(value != 0)
                case .autoPowerOff: try c.setAutoPowerOff(value)
                }
                let label = s.options.first(where: { $0.0 == value })?.1 ?? "\(value)"
                self.log("\(s.label) → \(label)")
            } catch {
                self.ui { self.values[s.key] = old }
                throw error
            }
        }
    }

    // MARK: gallery

    func loadFiles() {
        loadingFiles = true
        let added = addedHandles
        let ev = values["ev"] ?? 0
        let res = values["res"] ?? 0
        run("讀取檔案", showBusy: false) { c in
            // Like the app: browse mode while reading the card, then back to remote
            // control mode and re-send the live-view parameters.
            do { try c.setProp(Prop.functionalMode, 0x8001) } catch { self.log("切換瀏覽模式失敗：\(error.localizedDescription)") }
            defer {
                do {
                    try c.setProp(Prop.functionalMode, 0x8002)
                    if c.firmware > 11000 {
                        try c.setProp(Prop.exposureComp, Int64(ev))
                        try c.setProp(Prop.stillResolution, Int64(res))
                    }
                } catch {
                    self.log("切回遙控模式失敗：\(error.localizedDescription)")
                }
                self.ui { self.loadingFiles = false }
            }

            var listed: [UInt32] = []
            do { listed = try c.listHandles() } catch { self.log("讀取檔案清單失敗：\(error.localizedDescription)") }
            // newest first: files added this session, then the card's list (newest last)
            var order: [UInt32] = []
            for h in added.reversed() where !order.contains(h) { order.append(h) }
            for h in listed.reversed() where !order.contains(h) { order.append(h) }
            self.log("相簿：卡上清單 \(listed.count) 個，本次新增 \(added.count) 個")

            var list: [CameraFile] = []
            for h in order {
                if let oi = try? c.objectInfo(h) {
                    list.append(CameraFile(id: h, name: oi.filename, size: oi.size))
                }
            }
            let snapshot = list
            self.ui { self.files = snapshot }
            for f in snapshot where !f.isVideo {
                guard let bytes = try? c.thumbnail(f.id), let img = UIImage(data: Data(bytes)) else { continue }
                self.ui {
                    if let i = self.files.firstIndex(where: { $0.id == f.id }) { self.files[i].thumb = img }
                }
            }
        }
    }

    func download(_ file: CameraFile) {
        let url = CameraModel.documents.appendingPathComponent(file.name)
        updateFile(file.id) { $0.progress = 0 }
        run("下載 \(file.name)", showBusy: false) { c in
            do {
                try c.download(file.id, to: url) { p in
                    self.updateFile(file.id) { $0.progress = p }
                }
                self.updateFile(file.id) { $0.progress = nil; $0.localURL = url }
                self.log("已下載 \(file.name)")
            } catch {
                self.updateFile(file.id) { $0.progress = nil }
                throw error
            }
        }
    }

    func delete(_ file: CameraFile) {
        run("刪除 \(file.name)") { c in
            try c.delete(file.id)
            self.ui {
                self.files.removeAll { $0.id == file.id }
                self.addedHandles.removeAll { $0 == file.id }
            }
            self.log("已刪除 \(file.name)")
        }
    }

    func formatCard() {
        run("格式化") { c in
            try c.formatCard()
            self.ui {
                self.files = []
                self.addedHandles = []
            }
            self.log("記憶卡已格式化")
        }
    }

    func saveToPhotos(_ file: CameraFile) {
        guard let url = file.localURL else { return }
        guard canSaveToPhotos else {
            alertMessage = "要存到「照片」，請先在 Swift Playgrounds 的 App 設定裡加入「Photo Library Add」功能。目前可以用「存到檔案」。"
            return
        }
        if file.isVideo {
            if UIVideoAtPathIsCompatibleWithSavedPhotosAlbum(url.path) {
                UISaveVideoAtPathToSavedPhotosAlbum(url.path, nil, nil, nil)
                log("已存到照片：\(file.name)")
            } else {
                alertMessage = "這個影片格式無法存到「照片」，請用「存到檔案」"
            }
        } else if let img = UIImage(contentsOfFile: url.path) {
            UIImageWriteToSavedPhotosAlbum(img, nil, nil, nil)
            log("已存到照片：\(file.name)")
        }
    }

    private func updateFile(_ id: UInt32, _ change: @escaping (inout CameraFile) -> Void) {
        ui {
            if let i = self.files.firstIndex(where: { $0.id == id }) { change(&self.files[i]) }
        }
    }
}
