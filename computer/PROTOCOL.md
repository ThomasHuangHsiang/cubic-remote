# altek Cubic (C01) 通訊協定筆記

從 Cubic Camera 1.3.29 (`com.altek.camera_control_app`) 反編譯整理。全部是 Java，沒有原生函式庫，也沒有加密。

## 連線

| 項目 | 值 |
|---|---|
| 相機 Wi-Fi | 相機自己開 AP，SSID 以 `Cubic` 開頭 |
| 相機 IP | `192.168.0.1` |
| 控制 | PTP/IP（ISO 15740），TCP `15740`，兩條連線（command + event） |
| 即時預覽 | RTSP `rtsp://192.168.0.1/MJPG`（TCP 554）→ RTP/JPEG (RFC 2435) 送到手機的 UDP `65362` |

## PTP/IP 握手（照 app 的寫法）

1. Command 連線送 `InitCommandRequest`：
   - GUID = `"cubic"` + 11 個 `0xFC`
   - Friendly name = 手機 Wi-Fi MAC 字串，**直接用 ASCII**，沒有 UTF-16、沒有結尾 NUL（不合標準，但 app 就是這樣送）
   - Protocol version = `0x00013000`
2. 收到 `InitCommandAck`（type 2）
3. 開第二條連線送 `InitEventRequest`，connection number **寫死 1**
4. 收到 `InitEventAck`（type 4）
5. `OpenSession`（transaction id 0，param 1）
6. 每秒在 command 連線送一次 Probe（type 13），相機回 type 14，當作 keep-alive

app 的怪癖（我們照抄）：
- Operation Request 的 DataPhaseInfo 永遠是 0，即使後面有資料
- 所有數值屬性一律用 4 bytes INT32 寫入
- 字串屬性格式：`[字元數][UTF-16LE]`，沒有 NUL

## 連線後的初始化順序

1. `SetDevicePropValue(0x5002, 0x8002)`：切到遙控模式
2. `0x90D8 GetDSCInfo`：response param0 = 韌體編號（例如 11500）。app 拒絕 7001–7530 的版本
3. `GetStorageIDs`：`0x00010001` = 有 microSD，否則是內建記憶體
4. 讀電量 `0x5001`（UINT8）
5. 讀屬性描述：0x500C、0x5071、0x5091、0x5031
6. `0x90D3`(1) 可拍張數
7. 韌體 > 8000：`0x90DA` 讀自動關機時間、`0x90D9`(0) 關近拍
8. 韌體 > 11000：寫解析度、美肌、高畫質、亮度、`0x90DC` 自拍秒數
9. 開 RTSP

## 指令

| 代碼 | 名稱 | 參數 | 說明 |
|---|---|---|---|
| 0x100E | InitiateCapture | 2（韌體 > 10000）或 4 | 拍照 |
| 0x90B1 | S1 Lock | 1 = 有提示音 | 半按快門對焦，回傳對焦框資料 |
| 0x90B2 | S1 Release | | 放開快門 |
| 0x90B3 | Get QV Image | | 剛拍那張的預覽 JPEG |
| 0x90B5 | Touch Shot | x, y（預覽畫面座標） | 觸控對焦拍照 |
| 0x90B7 | Zoom | 方向（0 廣角 / 1 望遠）, 按下 1 / 放開 0 | 按住才會持續變焦 |
| 0x90C1 | Video Rec Key | | 開始 / 停止錄影（切換） |
| 0x90D3 | Storage Free | 1 = 張數, 2 = 錄影秒數 | 結果在 response param0 |
| 0x90D6 | Reset Auto Power Off | 秒數 | 60 / 300 / 57600（= 關閉） |
| 0x90D7 | Get Altek ID | | |
| 0x90D8 | Get DSC Info | | 韌體編號 |
| 0x90D9 | Set Macro | 0 / 1 | 近拍 |
| 0x90DA | Get Auto Power Off | | |
| 0x90DB | Agree Auto Power Off | 0 / 1 | |
| 0x90DC | Self Timer | 秒數（0 或 3） | |
| 0x90A0/A1/A2 | RTSP info / play / stop | | app 沒用到 |

拍照流程：S1 Lock → InitiateCapture → S1 Release。

**錄影也是用同一組快門指令**：先把 0x5031 切到錄影（enum[1]），再送一次上面的快門流程開始錄影，再送一次停止。`0x90C1 Video Rec Key` 只接在 app 裡被隱藏的按鈕上，真機上送了不會產生檔案（2026-10-25 實測）。停止錄影時 InitiateCapture 會回 `DeviceBusy`（0x2019），但相機其實已經在停止並存檔，約 1 秒後送出 `OBJECT_ADDED`；app 不檢查這個回應，S1 Release 一定要照送（2026-10-25 實測）。app 會在 YouTube 61 秒、Instagram 16 秒、Full HD 剩餘秒數時自動再按一次停止。第一次拍照前先把時間寫進 `0x5011`（`YYYYMMDDThhmmss`）。

下載：`GetPartialObject`，每次 1843200 bytes。

**檔案清單**：`GetObjectHandles` 在同一次連線中不會更新，新拍的檔案要重新連線才會出現（2026-10-26 實測）。app 的做法是每次連線只讀一次清單，再把 `OBJECT_ADDED` 事件回報的 handle 記下來放在最前面。進相簿前把 0x5002 設成 0x8001（瀏覽），離開時設回 0x8002，並重送亮度和解析度。

## 屬性

| 代碼 | 名稱 | 值 |
|---|---|---|
| 0x5001 | 電量 | 0–100 |
| 0x5002 | 功能模式 | 0x8002 遙控 / 0x8001 瀏覽 |
| 0x5003 | 相片解析度 | 0 = 13M, 1 = 10M, 2 = 8M |
| 0x500C | 閃光燈 | 1 自動, 2 關, 3 強制 |
| 0x5010 | 曝光補償（亮度） | 2000, 1000, 0, -1000, -2000 |
| 0x5011 | 日期時間 | 字串 |
| 0x5031 | 場景 | enum[0] = 拍照, enum[1] = 錄影 |
| 0x5032 | ISO | 1 自動, 2–7 = 100–3200 |
| 0x5033 | 快門 | app 未使用 |
| 0x5034 | 美肌 | 0 關, 1 弱, 2 中, 3 強 |
| 0x5035 | 儲存高解析度 | 0 / 1 |
| 0x5071 | 錄影尺寸 | enum[0] Full HD, [1] YouTube（≤61 秒）, [2] Instagram（≤16 秒） |
| 0x5081 | 音量 | 0 靜音 … 3 |
| 0x5091 | 觸控拍攝 | 1 開, 2 關 |

## 事件

| 代碼 | 意義 |
|---|---|
| 0x400D | 拍攝完成 |
| 0xC0A1 | QV（預覽圖）準備好 → 呼叫 0x90B3 |
| 0x4002 | 新增檔案，param1 = handle |
| 0x4006 | 屬性改變，param1 = 屬性代碼 |
| 0xC0A4 | 自拍倒數開始 |
| 0xC0A2 / 0xC0B2 | 關機 / 即將自動關機 |
| 0xC0B1 | 接上電源 |
| 0xC0B3 | 相機錯誤 |

## RTSP

app 依序送 OPTIONS、DESCRIBE（`Accept: Applecation/sdp`，拼錯是原樣）、SETUP（`client_port=65362-65363`）、PLAY（`Session:` 用自己產生的亂數，沒有讀 SETUP 的回應）。換行用 `\n`。

RTP payload type 26。**拼畫面不能靠 RTP timestamp**：原廠 app 只看序號、fragment offset = 0（開頭）和 marker 位元（結尾）。用 timestamp 分組在真機上拼不出畫面，改成 app 的方式後才正常（2026-10-25 實測）。

app 用固定的 JPEG 檔頭範本（4:2:0、標準 Huffman 表），Q < 128 時用 RFC 2435 公式算量化表，Q ≥ 128 時表格跟著封包一起傳。
