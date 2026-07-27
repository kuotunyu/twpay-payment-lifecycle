# 本機測試流程（ECPay Stage）

綠界的背景通知要送到可由外部連線的 HTTPS 網址，不能使用 localhost。
本機測試因此需要一條公開通道；回傳網址會逐筆帶在建單參數中，每次通道網址
改變時只要更新 `.env` 的 `BASE_URL`。

> 建議使用 cloudflared Quick Tunnel：免費且不需帳號。ngrok 也可使用。

## 事前準備

1. 複製 `.env.example` 為 `.env`，再依註解連結填入綠界官方公開測試特店資料。
   NewebPay 欄位可留空；測試金鑰仍只保存在 `.env`。
2. 安裝 cloudflared：`winget install Cloudflare.cloudflared`。
3. 安裝專案相依套件：`uv sync --all-groups`。

## 啟動

```powershell
# 終端機 1：啟動服務
uv run python -m uvicorn twpay_checkout.main:app --port 8000

# 終端機 2：建立公開通道，記下輸出的 https://xxxx.trycloudflare.com
cloudflared tunnel --url http://localhost:8000

# 終端機 3：更新 BASE_URL
uv run python scripts/update_tunnel_url.py https://xxxx.trycloudflare.com

# 回到終端機 1，以 Ctrl+C 停止後重新啟動服務
```

開啟 <http://localhost:8000> 操作商店；金流頁面完成後會回到本機訂單頁。

## 建議驗證路徑

| # | 路徑 | 操作 | 預期結果 |
|---|---|---|---|
| 1 | 信用卡單次付款 | 測試卡 `4311-9511-1111-1111`、未來效期、CVV `123`、OTP `1234` | ReturnURL 驗簽與金額核對通過，訂單成為 `paid` |
| 2 | ATM 取號與入帳 | 完成取號，再到綠界測試後台對該筆執行模擬付款 | `pending → awaiting_payment → paid` |
| 3 | 定期定額 | 選擇「每月扣款 3 期」，用同一張測試卡與 OTP 完成首期 | 訂單 `paid`、訂閱 `active`、成功期數 1/3 |
| 4 | 主動查詢復原 | 到 `/admin/operations` 查詢單次訂單或訂閱 | 同步綠界狀態，必要時補回本機漏記的首期扣款 |
| 5 | 終止定期扣款 | 在營運台終止仍為 `active` 的訂閱 | 綠界回覆成功，本機訂閱成為 `canceled`；再次查詢應顯示 `ExecStatus=0` |
| 6 | 退款策略 | 對已付款訂單送出退款模擬 | 建立可稽核、冪等的 `SIMULATED` 紀錄，不呼叫正式環境 API |
| 7 | 對帳 | 在營運台上傳 ECPay CSV，或使用 CLI 匯入 | 產生 matched／金額差異／狀態差異／單邊缺漏報告，不修改訂單 |

定期定額第二期之後的 `PeriodReturnURL` 無法在同一天等待真實月週期觸發；
成功、失敗、重複通知與金額不符等情境由整合測試覆蓋。

## 對帳 CLI

```powershell
# 匯入既有 CSV
uv run python scripts/reconcile_ecpay.py --file .\ecpay-report.csv --begin 2026-07-01 --end 2026-07-31

# 下載並匯入測試環境前一天報表
uv run python scripts/reconcile_ecpay.py --download-stage
```

## 自動驗證

```powershell
uv run python -m pytest -q
uv build
```

## 疑難排解

- **收不到通知**：確認公開通道仍存活、`.env` 的 `BASE_URL` 是最新網址，且服務已重新啟動。
- **通知持續重送**：綠界未收到 `1|OK`；請查看伺服器紀錄與通知稽核頁。
- **Stage 偶發 5xx**：稍後重試，並以主動查詢判斷交易最終狀態。
- **無法從 Stage 測退款**：綠界退款 API 沒有測試端點；本專案刻意只做政策模擬，避免誤打正式環境。

已完成的真實測試紀錄見 [e2e-results.md](e2e-results.md)。
