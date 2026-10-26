# Cubic Remote

altek Cubic（C01）相機的遙控程式，取代已下架的 Cubic Camera app。通訊協定由原廠 app 1.3.29 反編譯而來，細節見 [computer/PROTOCOL.md](computer/PROTOCOL.md)。

- `CubicRemote.swiftpm/`：iPad / iPhone 版（Swift）。iPad 可直接用 Swift Playgrounds 打開執行。
- `computer/`：電腦版（Python，瀏覽器操作）。
- `.github/workflows/build-ipa.yml`：每次推送到 `main` 時，在 GitHub 的雲端 Mac 編譯 iPhone 安裝檔（未簽章 .ipa），可在 Actions 頁面的 Artifacts 下載，再用 Sideloadly 以免費 Apple ID 安裝。
