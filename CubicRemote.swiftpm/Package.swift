// swift-tools-version: 5.8

// WARNING:
// This is a Swift Playgrounds app project. Open it with Swift Playgrounds on iPad
// (or Xcode on a Mac). Changes made in the Playgrounds "App Settings" UI are saved here.

import PackageDescription
import AppleProductTypes

let package = Package(
    name: "Cubic Remote",
    platforms: [
        .iOS("16.0")
    ],
    products: [
        .iOSApplication(
            name: "Cubic Remote",
            targets: ["AppModule"],
            bundleIdentifier: "tw.thomas.cubicremote",
            displayVersion: "1.0",
            bundleVersion: "1",
            supportedDeviceFamilies: [
                .pad,
                .phone
            ],
            supportedInterfaceOrientations: [
                .portrait,
                .landscapeRight,
                .landscapeLeft,
                .portraitUpsideDown(.when(deviceFamilies: [.pad]))
            ]
        )
    ],
    targets: [
        .executableTarget(
            name: "AppModule",
            path: "."
        )
    ]
)
