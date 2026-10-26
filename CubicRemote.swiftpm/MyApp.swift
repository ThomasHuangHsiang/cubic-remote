import SwiftUI

@main
struct CubicRemoteApp: App {
    @StateObject private var model = CameraModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
        }
    }
}
