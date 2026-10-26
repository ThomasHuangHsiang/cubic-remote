import SwiftUI
import UIKit

let accent = Color(red: 0.31, green: 0.76, blue: 0.78)

struct ContentView: View {
    @EnvironmentObject var model: CameraModel
    @Environment(\.horizontalSizeClass) var sizeClass

    var body: some View {
        Group {
            if sizeClass == .regular {
                HStack(spacing: 0) {
                    MainArea()
                    Divider()
                    SidePanel().frame(width: 360)
                }
            } else {
                VStack(spacing: 0) {
                    MainArea().frame(maxHeight: 560)
                    Divider()
                    SidePanel()
                }
            }
        }
        .background(Color(white: 0.07).ignoresSafeArea())
        .preferredColorScheme(.dark)
        .onAppear {
            if model.state == .disconnected { model.connect() }
        }
        .alert("提示", isPresented: Binding(
            get: { model.alertMessage != nil },
            set: { if !$0 { model.alertMessage = nil } }
        )) {
            Button("好", role: .cancel) {}
        } message: {
            Text(model.alertMessage ?? "")
        }
    }
}

// MARK: - Main area

struct MainArea: View {
    var body: some View {
        VStack(spacing: 12) {
            StatusBar()
            ViewerView()
            ControlsView()
        }
        .padding(12)
    }
}

struct StatusBar: View {
    @EnvironmentObject var model: CameraModel

    var stateText: String {
        switch model.state {
        case .disconnected: return "未連線"
        case .connecting: return "連線中…"
        case .connected: return "已連線"
        case .failed: return "連線失敗"
        }
    }

    var stateColor: Color {
        switch model.state {
        case .connected: return .green
        case .connecting: return .orange
        case .failed: return .red
        case .disconnected: return .gray
        }
    }

    var leftText: String {
        if !model.isConnected { return "–" }
        if model.videoMode {
            let s = model.info.videoSecondsLeft
            return String(format: "%02d:%02d", s / 60, s % 60)
        }
        return "\(model.info.photosLeft) 張"
    }

    var body: some View {
        HStack(spacing: 14) {
            Text("Cubic").bold() + Text(" Remote").bold().foregroundColor(accent)
            HStack(spacing: 6) {
                Circle().fill(stateColor).frame(width: 9, height: 9)
                Text(stateText).font(.subheadline.weight(.semibold))
            }
            if model.isConnected {
                Label("\(model.info.battery)%", systemImage: "battery.75").font(.subheadline)
                Label(leftText, systemImage: model.videoMode ? "video" : "photo").font(.subheadline)
            }
            Spacer()
            if model.busy { ProgressView() }
            Button(model.isConnected ? "中斷" : "連線") {
                if model.isConnected { model.disconnect() } else { model.connect() }
            }
            .buttonStyle(.borderedProminent)
            .tint(accent)
            .disabled(model.state == .connecting)
        }
    }
}

struct ViewerView: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        GeometryReader { geo in
            ZStack {
                Color.black
                if let img = model.liveImage, model.isConnected {
                    Image(uiImage: img)
                        .resizable()
                        .scaledToFit()
                        .frame(width: geo.size.width, height: geo.size.height)
                } else {
                    Placeholder()
                }
            }
            .contentShape(Rectangle())
            .onTapGesture(coordinateSpace: .local) { point in
                handleTap(point, in: geo.size)
            }
            .overlay(alignment: .topLeading) { RecBadge().padding(12) }
            .overlay(alignment: .topTrailing) {
                if model.isConnected {
                    Text(model.videoMode ? "錄影" : "拍照")
                        .font(.footnote)
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .background(Color.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 8))
                        .padding(12)
                }
            }
            .overlay(alignment: .bottomLeading) { LastShotThumb().padding(12) }
        }
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    /// Maps a tap to live-view pixel coordinates (what the app sends for touch shot).
    private func handleTap(_ p: CGPoint, in size: CGSize) {
        guard model.isConnected, !model.videoMode, model.values["touch"] == 1,
              let img = model.liveImage, img.size.width > 0, img.size.height > 0 else { return }
        let scale = min(size.width / img.size.width, size.height / img.size.height)
        let w = img.size.width * scale, h = img.size.height * scale
        let ox = (size.width - w) / 2, oy = (size.height - h) / 2
        guard p.x >= ox, p.x <= ox + w, p.y >= oy, p.y <= oy + h else { return }
        let x = Int((p.x - ox) / scale), y = Int((p.y - oy) / scale)
        model.touchShot(x: x, y: y)
    }
}

struct Placeholder: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        VStack(spacing: 12) {
            switch model.state {
            case .connecting:
                ProgressView()
                Text("連線中…").foregroundColor(.secondary)
            case .connected:
                ProgressView()
                Text("已連線，等待即時畫面…")
                Text(model.liveStats).font(.footnote).foregroundColor(.secondary)
                Button("重新啟動預覽") { model.restartLive() }
                    .buttonStyle(.bordered)
            default:
                Text("尚未連線到相機").font(.title3.weight(.semibold))
                VStack(alignment: .leading, spacing: 6) {
                    Text("1. 打開 Cubic 電源")
                    Text("2. 到「設定 → Wi-Fi」連上 Cubic 開頭的網路")
                    Text("3. 回到這裡按「連線」")
                }
                .foregroundColor(.secondary)
                if !model.errorText.isEmpty {
                    Text("錯誤：\(model.errorText)").font(.footnote).foregroundColor(.red)
                        .multilineTextAlignment(.center)
                }
            }
        }
        .padding()
    }
}

struct RecBadge: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        if model.recording {
            TimelineView(.periodic(from: .now, by: 1)) { ctx in
                let s = max(0, Int(ctx.date.timeIntervalSince(model.recStart)))
                HStack(spacing: 6) {
                    Circle().fill(Color.red).frame(width: 10, height: 10)
                    Text(String(format: "%02d:%02d / %02d:%02d", s / 60, s % 60,
                                model.recLimit / 60, model.recLimit % 60))
                        .monospacedDigit()
                }
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(Color.black.opacity(0.6), in: RoundedRectangle(cornerRadius: 8))
            }
        }
    }
}

struct LastShotThumb: View {
    @EnvironmentObject var model: CameraModel
    @State private var showFull = false

    var body: some View {
        if let img = model.lastShot {
            Image(uiImage: img)
                .resizable()
                .scaledToFill()
                .frame(width: 96, height: 72)
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.8), lineWidth: 2))
                .onTapGesture { showFull = true }
                .sheet(isPresented: $showFull) {
                    VStack {
                        Image(uiImage: img).resizable().scaledToFit()
                        Button("關閉") { showFull = false }.padding()
                    }
                    .background(Color.black)
                }
        }
    }
}

// MARK: - Controls

struct ControlsView: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        HStack(spacing: 28) {
            HStack(spacing: 0) {
                HoldButton(title: "W") { pressed in model.zoom(tele: false, press: pressed) }
                Divider().frame(height: 30)
                HoldButton(title: "T") { pressed in model.zoom(tele: true, press: pressed) }
            }
            .background(Color(white: 0.12), in: Capsule())
            .overlay(Capsule().stroke(Color(white: 0.25)))

            ShutterButton()

            Picker("模式", selection: Binding(
                get: { model.videoMode },
                set: { model.setMode(video: $0) }
            )) {
                Text("拍照").tag(false)
                Text("錄影").tag(true)
            }
            .pickerStyle(.segmented)
            .frame(width: 140)
        }
        .disabled(!model.isConnected)
        .opacity(model.isConnected ? 1 : 0.4)
    }
}

/// Sends press on touch-down and release on touch-up, like the app's W/T buttons.
struct HoldButton: View {
    let title: String
    let action: (Bool) -> Void
    @State private var held = false

    var body: some View {
        Text(title)
            .font(.title3.bold())
            .frame(width: 64, height: 48)
            .background(held ? accent : Color.clear)
            .foregroundColor(held ? .black : .white)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { _ in
                        if !held { held = true; action(true) }
                    }
                    .onEnded { _ in
                        if held { held = false; action(false) }
                    }
            )
    }
}

struct ShutterButton: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        Button {
            model.shutter()
        } label: {
            ZStack {
                Circle().stroke(Color.white, lineWidth: 4).frame(width: 78, height: 78)
                if model.videoMode {
                    RoundedRectangle(cornerRadius: model.recording ? 6 : 30)
                        .fill(Color.red)
                        .frame(width: model.recording ? 30 : 60, height: model.recording ? 30 : 60)
                } else {
                    Circle().fill(Color.white).frame(width: 60, height: 60)
                }
            }
            .opacity(model.busy ? 0.5 : 1)
        }
        .buttonStyle(.plain)
        .disabled(model.busy)
    }
}

// MARK: - Side panel

struct SidePanel: View {
    @State private var tab = 0

    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $tab) {
                Text("設定").tag(0)
                Text("相簿").tag(1)
                Text("紀錄").tag(2)
            }
            .pickerStyle(.segmented)
            .padding(12)
            Divider()
            switch tab {
            case 0: SettingsPane()
            case 1: GalleryPane()
            default: LogPane()
            }
        }
        .background(Color(white: 0.09))
    }
}

struct SettingsPane: View {
    @EnvironmentObject var model: CameraModel
    @State private var confirmFormat = false

    var body: some View {
        List {
            Section {
                ForEach(allSettings) { s in
                    SettingRow(setting: s)
                }
            }
            Section {
                Button("重新整理電量與剩餘空間") { model.refresh() }
                Button("格式化記憶卡", role: .destructive) { confirmFormat = true }
            }
        }
        .listStyle(.insetGrouped)
        .disabled(!model.isConnected)
        .confirmationDialog("確定要格式化記憶卡？卡上所有照片和影片都會刪除，無法復原。",
                            isPresented: $confirmFormat, titleVisibility: .visible) {
            Button("格式化", role: .destructive) { model.formatCard() }
            Button("取消", role: .cancel) {}
        }
    }
}

struct SettingRow: View {
    @EnvironmentObject var model: CameraModel
    let setting: SettingDef

    var body: some View {
        Picker(setting.label, selection: Binding(
            get: { model.values[setting.key] ?? setting.defaultValue },
            set: { model.set(setting, $0) }
        )) {
            ForEach(0..<setting.options.count, id: \.self) { i in
                Text(setting.options[i].1).tag(setting.options[i].0)
            }
        }
        .pickerStyle(.menu)
    }
}

struct ExportItem: Identifiable {
    let id = UUID()
    let url: URL
}

struct GalleryPane: View {
    @EnvironmentObject var model: CameraModel
    @State private var exportItem: ExportItem? = nil
    @State private var toDelete: CameraFile? = nil

    let columns = [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)]

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(model.files.isEmpty ? "" : "共 \(model.files.count) 個檔案")
                    .font(.footnote).foregroundColor(.secondary)
                Spacer()
                if model.loadingFiles { ProgressView().padding(.trailing, 6) }
                Button("載入檔案") { model.loadFiles() }
                    .buttonStyle(.bordered)
                    .disabled(!model.isConnected || model.loadingFiles)
            }
            .padding(12)
            ScrollView {
                LazyVGrid(columns: columns, spacing: 10) {
                    ForEach(model.files) { f in
                        FileCell(file: f,
                                 onExport: { url in exportItem = ExportItem(url: url) },
                                 onDelete: { toDelete = f })
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 12)
            }
        }
        .sheet(item: $exportItem) { item in
            ExportPicker(url: item.url)
        }
        .confirmationDialog("刪除這個檔案？", isPresented: Binding(
            get: { toDelete != nil },
            set: { if !$0 { toDelete = nil } }
        ), titleVisibility: .visible) {
            Button("刪除", role: .destructive) {
                if let f = toDelete { model.delete(f) }
                toDelete = nil
            }
            Button("取消", role: .cancel) { toDelete = nil }
        } message: {
            Text(toDelete?.name ?? "")
        }
    }
}

struct FileCell: View {
    @EnvironmentObject var model: CameraModel
    let file: CameraFile
    let onExport: (URL) -> Void
    let onDelete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ZStack {
                Color.black
                if let t = file.thumb {
                    Image(uiImage: t).resizable().scaledToFill()
                } else {
                    Image(systemName: file.isVideo ? "video" : "photo")
                        .font(.title2).foregroundColor(.secondary)
                }
            }
            .frame(height: 110)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: 8))

            Text(file.name).font(.caption).lineLimit(1)
            Text(file.sizeText).font(.caption2).foregroundColor(.secondary)

            if let p = file.progress {
                ProgressView(value: p)
            } else if let url = file.localURL {
                HStack {
                    Button("存到檔案") { onExport(url) }
                    Spacer()
                    Button("存到照片") { model.saveToPhotos(file) }
                }
                .font(.caption)
            } else {
                HStack {
                    Button("下載") { model.download(file) }
                    Spacer()
                    Button("刪除", role: .destructive) { onDelete() }
                }
                .font(.caption)
            }
        }
        .padding(8)
        .background(Color(white: 0.13), in: RoundedRectangle(cornerRadius: 10))
        .buttonStyle(.borderless)
    }
}

struct LogPane: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(model.logs.enumerated()), id: \.offset) { item in
                        Text(item.element)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundColor(.secondary)
                            .textSelection(.enabled)
                            .id(item.offset)
                    }
                }
                .padding(12)
            }
            .onChange(of: model.logs.count) { n in
                if n > 0 { proxy.scrollTo(n - 1, anchor: .bottom) }
            }
        }
    }
}

/// "Save to Files" without needing any photo-library permission.
struct ExportPicker: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        UIDocumentPickerViewController(forExporting: [url], asCopy: true)
    }

    func updateUIViewController(_ controller: UIDocumentPickerViewController, context: Context) {}
}
