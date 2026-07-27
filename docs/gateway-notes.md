# 金流商規格筆記（ECPay AIO × NewebPay MPG，測試環境）

> 調查日期：**2026-07-17**。規格一律以官方文件為準；本文所有「官方範例值」均已在本專案 venv 以 Python 重算驗證（見各節標註）。
> 藍新現行官方手冊 PDF 存於 `docs/vendor/NDNF-1.2.3.pdf`（gitignore，僅本地參考）。
>
> 來源優先序：官方線上文件／官方 PDF 手冊 > 官方程式碼（GitHub、WordPress 外掛）> 第三方轉錄（僅交叉參考，文中標註）。

---

## 一、綠界 ECPay — 全方位金流 AIO

### 1. 版本與端點

| API | 版本 | 測試環境（本專案唯一使用） | 正式環境（本專案**禁止**呼叫） |
|---|---|---|---|
| 產生訂單 AioCheckOut | V5 | `https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5` | `https://payment.ecpay.com.tw/...` |
| 查詢訂單 QueryTradeInfo | V5 | `https://payment-stage.ecpay.com.tw/Cashier/QueryTradeInfo/V5` | `https://payment.ecpay.com.tw/...` |

- 皆為 HTTP POST、`Content-Type: application/x-www-form-urlencoded`。
- 產生訂單：<https://developers.ecpay.com.tw/?p=2862>；查詢訂單：<https://developers.ecpay.com.tw/2890/>。
- QueryTradeInfo：`TimeStamp` 為 Unix timestamp（有效區間約 3 分鐘）；`TradeStatus`：`0` 未付款、`1` 已付款、`10200095` 訂單不成立。呼叫過快會收 HTTP 403（官方：降速並等 30 分鐘）；信用卡建議付款後 10 分鐘再查。

### 2. CheckMacValue 演算法（檢查碼機制 <https://developers.ecpay.com.tw/2902/>）

1. 除 `CheckMacValue` 外所有參數依名稱 **A–Z 字典序**（不分大小寫）排序，以 `key=value` 用 `&` 串接。
2. 最前面加 `HashKey={HashKey}&`，最後面加 `&HashIV={HashIV}`。
3. 整串做 **URL encode**，結果必須符合官方「URLENCODE 轉換表」的 **.NET 編碼(ecpay)** 欄位（<https://developers.ecpay.com.tw/?p=7446>）。
4. 全字串**轉小寫** → **SHA256** → 雜湊值**轉大寫**＝CheckMacValue。

**URL encode 轉換表重點（Python 實作要照做）**：

| 規則 | 字元 |
|---|---|
| encode 後必須「還原」成原字元 | `-` `_` `.` `!` `*` `(` `)` |
| 空白 | 轉成 `+` |
| **保持編碼**（Python `quote_plus` 預設不編它！） | `~` → `%7e` |
| 其餘符號 | 維持 `%xx` |

Python 落地：`urllib.parse.quote_plus()` → 轉小寫 → 把 `%21 %2a %28 %29 %2d %5f %2e` 還原成 `! * ( ) - _ .` → 把 `~` 換成 `%7e`。中文以 UTF-8 bytes 編碼。

**驗證回傳通知**：把通知 payload 移除 `CheckMacValue` 後重算、與通知值比對（不分大小寫比對前先統一大寫）。通知欄位值含 `%26`(&)、`%3C`(<) 時官方註明需先 urldecode（<https://developers.ecpay.com.tw/?p=2858>）。

### 3. 官方範例值（單元測試 fixture；✅ 2026-07-17 本機重算一致）

```
HashKey = pwFHCqoQZGmho4w6
HashIV  = EkRm7iFT261dpevs
參數：MerchantID=3002607、MerchantTradeNo=ecpay20230312153023、
     MerchantTradeDate=2023/03/12 15:30:23、PaymentType=aio、TotalAmount=30000、
     TradeDesc=促銷方案、ItemName=Apple iphone 15、
     ReturnURL=https://www.ecpay.com.tw/receive.php、ChoosePayment=ALL、EncryptType=1
期望 CheckMacValue = 6C51C9E6888DE861FD62FB1DD17029FC742634498FD813DC43D4243B5685B840
```

補充 fixture 來源：官方 GitHub [ECPay/ECPay-API-Skill](https://github.com/ECPay/ECPay-API-Skill)（2026-07 仍在更新）內含 25 組跨語言加密測試向量，M3 寫單元測試時可再抽幾組。官方 Python SDK [ECPay/ECPayAIO_Python](https://github.com/ECPay/ECPayAIO_Python) 僅作交叉參考（2022 後低維護）。

### 4. 測試環境

- **測試商店（官方公開，<https://developers.ecpay.com.tw/2856/>）**：MerchantID **3002607**（信用卡有模擬 3D 驗證）；HashKey/HashIV 即第 3 節 fixture 值 → 已填入本專案 `.env`。
  - ⚠️ 舊教學常見的 **2000132 已不在現行 AIO 文件**；網路流傳的 `ejCk326UnaZWKisg/q9jcZX8Ib9LM8wYk` 是**電子發票**測試金鑰，勿混用。
- **測試廠商後台**：<https://vendor-stage.ecpay.com.tw/>，帳號 `stagetest3`／密碼 `test1234`／統編 `00000000`。
- **測試信用卡**：`4311-9511-1111-1111`、`4311-9522-2222-2222`（海外卡 `4000-2011-1111-1111`）；安全碼任意三碼；效期填未來月年；**3D 驗證簡訊 OTP 固定 `1234`**。
- **ATM 模擬入帳**：取號後登入測試後台 →「一般訂單查詢 > 全方位金流訂單」→ 該筆訂單按「**模擬付款**」→ 綠界發付款結果通知（`RtnCode=1`、`SimulatePaid=1`，不會撥款）。

### 5. 回傳網址與通知語意

| 參數（皆逐筆帶在建單請求中） | 性質 | 商店應回應 |
|---|---|---|
| `ReturnURL` | **付款結果背景通知**（Server POST） | 純文字 **`1|OK`**。錯誤示範：`"1|OK"`、`1|ok`、`_OK`、空白。未正確回應→隔 5–15 分鐘重發、**當天最多 4 次**（<https://developers.ecpay.com.tw/?p=2878>） |
| `PaymentInfoURL` | **ATM/CVS 取號結果背景通知**（POST） | 同樣 **`1|OK`**（<https://developers.ecpay.com.tw/?p=2881>） |
| `OrderResultURL` | 前景導回（瀏覽器 **POST** 付款結果） | 無需特定回應。**不支援銀聯卡與非即時付款（ATM/CVS/BARCODE）** |
| `ClientRedirectURL` | ATM/CVS 取號完成後前景導回 | 無；設定後 ClientBackURL 按鈕失效 |
| `ClientBackURL` | 綠界頁「返回特店」按鈕（不帶結果） | 無 |

- **RtnCode 語意**：付款成功通知 `RtnCode=1`（唯一成功值）；**ATM 取號成功 `RtnCode=2`**（走 PaymentInfoURL）；CVS/BARCODE 取號成功 `10100073`；其餘視為失敗。
- `SimulatePaid=1`＝後台模擬付款（RtnCode 仍為 1、不撥款）——demo 標註用，正式環境收到不可出貨。
- 回呼網址限制（<https://developers.ecpay.com.tw/?p=2858>）：必須對外可連線（不可 localhost）、**僅支援 port 80/443**、不支援中文網址、TLS 1.2 → ngrok https 網址符合。
- 官方明文要求商店端**冪等**：「判斷是否已經對該筆訂單的付款通知做過相對應的處理」。

### 6. ATM 參數（<https://developers.ecpay.com.tw/2872/>）

- 建單：`ChoosePayment=ATM`；`ExpireDate`＝繳費期限，**Int、單位「天」、預設 3、範圍 1–60**。
- 取號通知（PaymentInfoURL）：`BankCode` String(3)、`vAccount` String(16)、`ExpireDate` String(20)（格式 `yyyy/MM/dd`）＋ MerchantTradeNo/TradeNo/RtnCode/TradeAmt/CheckMacValue 等。

### 7. 欄位限制與注意事項

- `MerchantTradeNo`：**String(20)**、英數字、唯一不可重複；付款失敗重付必須換新編號。
- `TotalAmount`：**Int**（整數新台幣，不可小數）；通知的 `TradeAmt` 亦為 Int → 入帳前必比對 DB 訂單金額。
- 參數不允許 HTML tag 與特殊符號；`TradeDesc` String(200) 勿帶特殊字元；`ItemName` String(400)、多商品以 `#` 分隔（超長截斷會造成 CheckMacValue 錯誤）。
- API timeout 官方建議 ≥30 秒。

### 8. 付款生命週期擴充（2026-07-26）

本階段把可公開展示的主線改為「ECPay 完整付款生命週期」。藍新 adapter 與官方測試向量仍保留，但因藍新測試商店需以真實身分註冊且只有 30 天效期，不再是 demo 執行條件；README 必須明確標示其為 contract-tested，而不是 live E2E。

#### 8.1 定期定額

- 建單仍使用 AIO V5 測試端點，`ChoosePayment=Credit`。
- 必填週期欄位：`PeriodAmount`（須等於 `TotalAmount`）、`PeriodType`（`D`／`M`／`Y`）、`Frequency`、`ExecTimes`、`PeriodReturnURL`。
- `Frequency` 範圍：日繳 1–365、月繳 1–12、年繳固定 1。
- 第一次授權結果走一般 `ReturnURL`；第二期起每期結果送到 `PeriodReturnURL`，應回純文字 `1|OK`。
- 每期通知包含 `MerchantTradeNo`、`TradeNo`、`PeriodType`、`Frequency`、`ExecTimes`、`PeriodAmount`、`TotalSuccessTimes`、`TotalSuccessAmount` 等。通知只發送一次，因此漏單仍須由查詢／對帳補償。
- 定期明細查詢測試端點：`https://payment-stage.ecpay.com.tw/Cashier/QueryCreditCardPeriodInfo`；可取得 `ExecStatus`、成功次數／金額與逐期 `ExecLog`。
- 終止後續扣款使用測試端點 `https://payment-stage.ecpay.com.tw/Cashier/CreditCardPeriodAction`，`Action=Cancel`；回應必須驗 `CheckMacValue`。終止成功後不可重新啟用，只能建立新計畫。`ReAuth` 雖使用同端點，但官方明載 stage 無法測試。
- 官方來源：<https://developers.ecpay.com.tw/2868/>、<https://developers.ecpay.com.tw/5631/>、<https://developers.ecpay.com.tw/2892/>、<https://developers.ecpay.com.tw/2900/>。

#### 8.2 主動查單與漏單補償

- 使用 `QueryTradeInfo/V5`，表單 POST `MerchantID`、`MerchantTradeNo`、三分鐘內的 `TimeStamp`、`CheckMacValue`。
- 回應必須先驗 `CheckMacValue`，再比對 `MerchantTradeNo` 與 DB 金額；只有 `TradeStatus=1` 且三項全部一致，才允許把本地 pending／awaiting_payment 補成 paid。
- 每次查詢（成功或失敗）都寫 immutable audit log；重複查詢不得重複套用狀態。
- 信用卡未收到通知時，依官方建議延後約 10 分鐘查詢；ATM 等離線付款仍以通知為主。遇到 403 應停止重試並降速，不能 busy loop。
- 官方來源：<https://developers.ecpay.com.tw/2890/>。

#### 8.3 退款／取消

- 信用卡操作前必須先查明細狀態，再選擇動作：已授權用 `N` 放棄；要關帳的全額退款依序 `E` 取消、`N` 放棄，部分退款用 `R`；已關帳用 `R` 退刷。
- **官方測試環境無法實際授權，因此沒有可呼叫的測試請退款端點**；正式端點是 `https://ecpayment.ecpay.com.tw/1.0.0/Credit/DoAction`，本專案禁止呼叫。
- 因此作品中的 refund workflow 只做狀態機、金額／冪等檢查、稽核紀錄與明確標示的 sandbox simulation；絕不以 mock 成功冒充 live E2E。正式 transport 預設關閉且不包含 production URL。
- 定期定額停用不能使用信用卡請退款 API，需使用定期定額訂單作業 API 或後台。
- 官方來源：<https://developers.ecpay.com.tw/35642/>。

#### 8.4 每日對帳

- 測試下載端點：`https://vendor-stage.ecpay.com.tw/PaymentMedia/TradeNoAio`，表單 POST。
- 主要欄位：`MerchantID`、`DateType`（2 付款日／4 撥款日／6 訂單日）、`BeginDate`、`EndDate`、`MediaFormated=2`（CSV V3）、`CharSet=2`（UTF-8）、`CheckMacValue`；可選 `PaymentType` 等篩選。
- 後台必須先設定下載 IP 白名單，且同 IP 每分鐘只能下載一個檔案。因此 portfolio demo 以「CSV 匯入 + 相同 parser／比對器」做可重現展示，另保留 stage downloader 的 request builder。
- 對帳以 `MerchantTradeNo` 對 DB 訂單，分類為 matched、missing_local、missing_gateway、amount_mismatch、status_mismatch；不能因匯入報表而直接改訂單狀態。
- 官方來源：<https://developers.ecpay.com.tw/2896/>。

---

## 二、藍新 NewebPay — MPG 幕前支付

### 1. 手冊版本與端點

- 現行手冊：**《線上交易─幕前支付技術串接手冊》NDNF-1.2.3（2026-07-14 發布）**，官方下載頁 <https://www.newebpay.com/website/Page/content/download_api>（對程式抓取回 403，需瀏覽器人工下載）。本地副本：`docs/vendor/NDNF-1.2.3.pdf`。
- `Version` 參數：手冊規格值 **2.3**；**2.0 完全相容**且官方範例程式自身用 2.0 → **本專案用 2.0**（TWQR／OrderDetail 等 2.2+/2.3 專屬功能不用）。

| API | 測試環境（本專案唯一使用） | 正式環境（**禁止**） | Version |
|---|---|---|---|
| MPG 建立交易（Form POST 導頁） | `https://ccore.newebpay.com/MPG/mpg_gateway` | `https://core.newebpay.com/...` | 2.0 |
| 查詢交易 QueryTradeInfo | `https://ccore.newebpay.com/API/QueryTradeInfo` | `https://core.newebpay.com/...` | 1.3 |

- ⚠️ 查詢 API 的檢查碼公式與 TradeSha **不同**（NDNF-1.2.3 §4.3）：
  `CheckValue = UPPER(SHA256("IV={HashIV}&Amt={amt}&MerchantID={mid}&MerchantOrderNo={no}&Key={HashKey}"))`——注意用 `IV=`／`Key=`。
- 查詢回傳 `TradeStatus`：`0` 未付款、`1` 付款成功、`2` 失敗、`3` 取消、`6` 退款。收不到 Notify 時以此對帳補單。

### 2. TradeInfo／TradeSha 演算法（NDNF-1.2.3 §4.1）

- **加密**：交易參數組成 UTF-8 query string（值需 URL encode）→ **AES-256-CBC＋PKCS7 填充** → `bin2hex` 小寫十六進位。key＝32 字元 HashKey、iv＝16 字元 HashIV（直接取 ASCII bytes，無 KDF）。
- `EncryptType` 不帶或 `0`＝CBC（`1`＝AES/GCM，官方未載明 wire format，**不使用**）。
- **TradeSha**＝`UPPER(SHA256("HashKey={HashKey}&{TradeInfo密文}&HashIV={HashIV}"))`。
- **解密（收通知）**：先驗 TradeSha → `hex2bin` → AES-256-CBC 解密 → **依最後一個 byte 值手動去 padding（容忍 1–32）**。官方手冊 §4.1.4 的 `strippadding()` 即此作法；官方 WordPress 外掛實作以 blocksize=32 padding，**不可**用嚴格的 16-byte PKCS7 unpadder。
- 三種回傳（Notify/Return/Customer）payload 同構：`Status`、`MerchantID`、`Version`、`TradeInfo`、`TradeSha`。一律先驗簽再解密；解密後 `Status=="SUCCESS"` 為成功。

### 3. 官方範例值（單元測試 fixture，NDNF-1.2.3 §4.1；✅ 2026-07-17 本機重算一致）

**請求方向（加密＋TradeSha）**：

```
HashKey = Fs5cX1TGqYM2PpdbE14a9H83YQSQF5jn
HashIV  = C6AcmfqJILwgnhIP
明文    = MerchantID=MS127874575&RespondType=String&TimeStamp=1695795410&Version=2.0&MerchantOrderNo=Vanespl_ec_1695795410&Amt=30&ItemDesc=test&NotifyURL=https%3A%2F%2Fwebhook.site%2Fd4db5ad1-2278-466a-9d66-78585c0dbadb
密文    = f79eac33c4f3245d58f17b544c5d38b09457a6d77e77bae6f10fcc7236fe153ccef1a80001c0746afc063a7570f80ad970d8a32c72332c9ec5547410188007876bdca2bafa52d07d31b6b183f2204d6e4feee6d245e286ab198cf95422ad5843c7696fc943cbb65979ad207607d4b5d97dac4a90ccd5e7a37adb7d7062e838be09d94e8c5dfa145c048e17feabe58c2e310792f0f50f5af32961ffb07ff6649ae1021ad558242551de5f09316e3182e198775e5d1ad5b66a70be290004de750fa85d86b0c2f087b40005d89e048be2ab6fd83f1c522494c093426a10a1f73fe4
TradeSha = 84E4D9F96537E029F8450BE1E759080F9AF6995921B7F6F9AAFDDD2C36E7B287
```

**Notify 回傳方向（驗簽＋解密）**：

```
密文（TradeInfo）=
ee11d1501e6dc8433c75988258f2343d11f4d0a423be672e8e02aaf373c53c2363aeffdb4992579693277359b3e449ebe644d2075fdfbc10150b1c40e7d24cb215febefdb85b16a5cde449f6b06c58a5510d31e8d34c95284d459ae4b52afc1509c2800976a5c0b99ef24cfd28a2dfc8004215a0c98a1d3c77707773c2f2132f9a9a4ce3475cb888c2ad372485971876f8e2fec0589927544c3463d30c785c2d3bd947c06c8c33cf43e131f57939e1f7e3b3d8c3f08a84f34ef1a67a08efe177f1e663ecc6bedc7f82640a1ced807b548633cfa72d060864271ec79854ee2f5a170aa902000e7c61d1269165de330fce7d10663d1668c711571776365bfdcd7ddc915dcb90d31a9f27af9b79a443ca8302e508b0dbaac817d44cfc44247ae613075dde4ac960f1bdff4173b915e4344bc4567bd32e86be7d796e6d9b9cf20476e4996e98ccc315f1ed03a34139f936797d971f2a3f90bc18f8a155a290bcbcf04f4277171c305bf554f5cba243154b30082748a81f2e5aa432ef9950cc9668cd4330ef7c37537a6dcb5e6ef01b4eca9705e4b097cf6913ee96e81d0389e5f775
TradeSha = C80876AEBAC0036268C0E240E5BFF69C0470DE9606EEE083C5C8DD64FDB3347A
解密後   = Status=SUCCESS&Message=%E6%8E%88%E6%AC%8A%E6%88%90%E5%8A%9F&MerchantID=MS127874575&Amt=30&TradeNo=23092714215835071&MerchantOrderNo=Vanespl_ec_1695795668&RespondType=String&IP=123.51.237.115&EscrowBank=HNCB&PaymentType=CREDIT&RespondCode=00&Auth=115468&Card6No=400022&Card4No=1111&Exp=2609&AuthBank=KGI&TokenUseStatus=0&InstFirst=0&InstEach=0&Inst=0&ECI=&PayTime=2023-09-27+14%3A21%3A59&PaymentMethod=CREDIT
```

（此筆範例 padding 為 9 bytes；解密實作仍需容忍到 32，理由見 §2。舊網路流傳的 spgateway 時代範例——HashKey=`1234…`——數學上自洽，可作次要測試向量，但非現行手冊內容。）

### 4. 測試環境

- **測試站**：<https://cwww.newebpay.com/> 註冊會員（與正式站完全獨立）→ 建立商店 →「商店管理」取得測試用 MerchantID／HashKey／HashIV → 填入 `.env`。
- ⚠️ **測試站會員帳密與測試資料自註冊日起僅 30 天有效**（官方手冊明文）→ 接近實測（M5/M7）時再申請。
- **測試信用卡只收 `4000-2211-1111-1111`**（一次付清＋分期；效期、末三碼任填），其他卡號一律失敗。另有紅利卡 `4003-5511-1111-1111`、AE `3760-000000-00006`。
- **非即時付款測試行為**：ATM（VACC）取號後系統會**自動**送出交易完成訊息（不需、也沒有手動模擬按鈕）——出自官方捐款平台手冊測試表，MPG 沙盒是否完全一致以實測為準。

### 5. 回傳網址與通知語意（NDNF-1.2.3 §4.2）

| 參數（帶在 TradeInfo 內，String(255)，僅 80/443 port） | 性質 | 商店應回應 |
|---|---|---|
| `NotifyURL` | **付款完成背景通知**（Server POST）——入帳唯一依據 | **HTTP 200**（body 不拘、不需 SUCCESS 字串）。Retry **3 次**都收不到 200 → 判定失敗並寄「Notify 觸發失敗通知信」（官方手冊常見問題明文） |
| `ReturnURL` | 付款完成前景導回（瀏覽器 **Form POST**） | 只做顯示 |
| `CustomerURL` | 非即時付款**取號完成**導回（帶加密取號結果） | 驗簽後僅允許 pending→awaiting_payment |

- **取號沒有背景通知**：官方 FAQ「須等繳費且銀行銷帳完成後才會回傳 Notify」；取號結果只走 CustomerURL。
- NotifyURL 與 ReturnURL 到達順序不保證；ReturnURL 可能因使用者關瀏覽器而不觸發 → 前端輪詢＋QueryTradeInfo 對帳兜底。

### 6. VACC（ATM 虛擬帳號）

- 啟用：TradeInfo 內 `VACC=1`；`ExpireDate` 格式 **`Ymd`**（例 `20260724`；預設 7 天、最長 180 天），另有 `ExpireTime`（His）。
- 取號結果（CustomerURL，`Status=SUCCESS` 表**取號**成功）：`BankCode`（金融機構代碼）、`CodeNo`（虛擬帳號）、`ExpireDate`／`ExpireTime` ＋共通欄位（Amt/TradeNo/MerchantOrderNo/PaymentType=VACC）。
- 入帳通知（NotifyURL）：共通欄位＋`PayBankCode`（付款人銀行）、`PayerAccount5Code`（付款帳號末五碼）。

### 7. 欄位限制與注意事項

- `MerchantOrderNo`：**String(30)**、限英數與底線 `_`、同商店不可重複（重複回 `MPG03008`）。
- `Amt`：Int(10)，新台幣整數。通知的 Amt 必比對 DB 訂單金額。
- `TimeStamp`：Unix 秒，與藍新伺服器**時差 ±120 秒內**（本機時鐘要準）。
- `ItemDesc`：String(50)、UTF-8、避免特殊符號；`TradeLimit` 交易有效秒數 60–900。
- 無官方 Python SDK；規格一律以 NDNF-1.2.3 PDF 為準，第三方 SDK 只作對照。

---

## 三、兩家差異對照表

| | 綠界 ECPay（AIO V5） | 藍新 NewebPay（MPG 2.0） |
|---|---|---|
| 簽章／加密 | CheckMacValue：參數排序＋HashKey/IV 包夾＋.NET 風格 URL encode＋SHA256 大寫（明文傳參） | 參數整包 AES-256-CBC 加密成 TradeInfo（hex）＋TradeSha=SHA256 大寫驗簽 |
| 背景通知 | `ReturnURL`（付款結果）＋`PaymentInfoURL`（取號結果） | `NotifyURL`（僅付款結果；**取號無背景通知**） |
| 通知成功回應 | 純文字 `1|OK` | HTTP 200（body 不拘） |
| 通知重送 | 5–15 分鐘、當天最多 4 次 | Retry 3 次 |
| 前景導回 | `OrderResultURL`（POST；不支援 ATM）／`ClientRedirectURL`（ATM 取號） | `ReturnURL`（POST）／`CustomerURL`（取號） |
| 成功判斷 | `RtnCode=1`（取號成功=2） | 解密後 `Status=="SUCCESS"` |
| 訂單編號 | `MerchantTradeNo` ≤20、英數 | `MerchantOrderNo` ≤30、英數＋底線 |
| 金額欄位 | `TotalAmount`／`TradeAmt`（Int） | `Amt`（Int） |
| ATM 繳費期限 | `ExpireDate`＝天數（Int，預設 3） | `ExpireDate`＝`Ymd` 日期（預設 7 天） |
| 測試金鑰 | 官方公開（3002607） | 自行到 cwww 註冊（30 天效期） |
| 測試卡 | 4311-9511-1111-1111 等，OTP 固定 1234 | 只收 4000-2211-1111-1111 |
| ATM 測試入帳 | 測試後台手動「模擬付款」（SimulatePaid=1） | 取號後自動發通知 |
| 官方 SDK | 有（Python，低維護；API-Skill 測試向量活躍） | 無（PDF 手冊附 PHP 範例） |

## 四、本專案測試流程摘要

1. 啟動 server（`uv run python -m uvicorn twpay_checkout.main:app`）與公開通道（cloudflared 或 ngrok），`uv run python scripts/update_tunnel_url.py` 更新 `.env` 的 `BASE_URL` 後重啟（詳見 `docs/local-testing.md`）。
2. **綠界信用卡**：商品頁下單 → 測試卡 `4311-9511-1111-1111`（CVV 任意、OTP `1234`）→ ReturnURL 背景通知 → 訂單 `paid`。
3. **綠界 ATM**：下單取號（PaymentInfoURL → `awaiting_payment`，頁面顯示虛擬帳號）→ vendor-stage 後台「模擬付款」→ ReturnURL（SimulatePaid=1）→ `paid`。
4. **藍新信用卡**：下單 → 測試卡 `4000-2211-1111-1111` → NotifyURL → `paid`。
5. **藍新 ATM（VACC）**：下單取號（CustomerURL → `awaiting_payment`）→ 沙盒自動發 NotifyURL → `paid`。
6. 全程可在 `/admin/notifications` 稽核頁看到每筆原始通知、驗簽結果、金額比對結果與處理結果。
