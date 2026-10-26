# Cubic Remote

用電腦控制 altek Cubic 相機，取代已下架的 Cubic Camera app。

通訊協定是從原廠 app（1.3.29）反編譯出來的，送出去的封包跟原廠 app 一樣，細節見 [PROTOCOL.md](PROTOCOL.md)。

## 需要

- Python 3.8 以上（只用到標準函式庫，不必 `pip install`）
- 有 Wi-Fi 的電腦（Windows / macOS / Linux 都可以）

## 使用方式

1. 打開相機電源
2. 電腦連上相機的 Wi-Fi，名稱以 `Cubic` 開頭（開放網路，不需要密碼）
3. 在這個資料夾裡執行：

   ```
   python cubic_web.py
   ```

4. 瀏覽器會自動打開 http://127.0.0.1:8080 ，看到即時畫面就可以操作了

功能：即時預覽、拍照（空白鍵也可以）、按住 W/T 變焦、錄影、觸控拍攝、所有原廠設定、相簿瀏覽、下載、刪除、格式化。拍完的預覽圖會自動存到 `photos/` 資料夾。

## 指令列版本（除錯用）

```
python cubic_cli.py -v info            # 連線並顯示狀態，-v 會印出每一步
python cubic_cli.py shoot --full       # 拍照並下載原圖
python cubic_cli.py zoom tele 0.5      # 望遠 0.5 秒
python cubic_cli.py record 10          # 錄 10 秒
python cubic_cli.py list               # 列出卡上的檔案
python cubic_cli.py download --all -o photos
python cubic_cli.py live --frames 30   # 存 30 張預覽畫面
python cubic_cli.py props              # 列出所有屬性
python cubic_cli.py raw 0x90D8         # 直接送任意指令
```

## 遇到問題

- **連線失敗**：確認電腦真的連在相機的 Wi-Fi 上，`ping 192.168.0.1` 要通。電腦連著相機時沒有網路是正常的。
- **連上了但沒有即時畫面**：相機是用 UDP 把畫面送到電腦的 65362 埠，防火牆常會擋。Windows 第一次執行時跳出防火牆視窗請按「允許」（私人和公用網路都勾）；macOS 請允許 Python 接受連入連線。
- **想看詳細過程**：用 `python cubic_cli.py -v info`，把輸出貼給我。

## 不用相機也能測試

`sim/fake_cubic.py` 是照原廠 app 行為寫的假相機（需要 `pip install pillow`）：

```
python sim/fake_cubic.py --rtsp-port 8554
python cubic_web.py --camera 127.0.0.1 --rtsp-port 8554
```

## 檔案

- `cubic/ptpip.py`：PTP/IP 用戶端（連線、指令、事件、下載）
- `cubic/liveview.py`：RTSP + RTP/JPEG 即時預覽
- `cubic_web.py` + `web/index.html`：瀏覽器介面
- `cubic_cli.py`：指令列工具
- `sim/fake_cubic.py`：假相機
