# Sandbox E2E 驗證紀錄

最後更新：2026-07-27（Asia/Taipei）

這份紀錄區分「pytest 模擬通知」與「金流商真的從 sandbox 發出通知」。
公開文件不記錄金鑰、完整虛擬帳號或其他可重用憑證。

## 已完成

| 金流商 | 付款方式 | 實測結果 | 驗證重點 |
|---|---|---|---|
| 綠界 ECPay | 信用卡 | 通過 | 官方測試卡與 3D OTP 完成付款；背景通知通過 CheckMacValue、金額核對並把訂單更新為 `paid` |
| 綠界 ECPay | ATM 虛擬帳號 | 通過 | 取號通知把訂單更新為 `awaiting_payment`；測試後台「模擬付款」送出第二次通知後更新為 `paid` |
| 綠界 ECPay | 主動查單 | 通過 | `QueryTradeInfo/V5` 回應通過 CheckMacValue、MerchantTradeNo 與金額三重比對；兩筆信用卡為 `TradeStatus=1`，未付款 ATM 為 `0` |
| 綠界 ECPay | 定期定額初次授權 | 通過 | 官方付款頁顯示每月 NT$450 × 3；測試卡＋3D OTP 後，ReturnURL 驗簽入帳並啟用計畫 |
| 綠界 ECPay | 定期明細查詢 | 通過 | `QueryCreditCardPeriodInfo` 回 `ExecStatus=1`、成功 1 期；補回首期 charge ledger |
| 綠界 ECPay | 終止後續扣款 | 通過 | `CreditCardPeriodAction Cancel` 回應驗簽通過；再查遠端 `ExecStatus=0` |

ATM 實測確認的狀態與稽核軌跡：

```text
pending
  → awaiting_payment
    notification: atm_account_issued
    signature: passed
    amount: matched
    result: applied
  → paid
    notification: payment_result
    signature: passed
    amount: matched
    result: applied
```

兩次通知端點皆回 HTTP 200；ECPay 收到的成功 body 為 `1|OK`。`docs/demo.gif`
由本次 sandbox 流程的實際畫面製作，虛擬帳號已遮蔽。

### 主動查單實測

2026-07-26 對三筆既有 sandbox 訂單呼叫官方 stage：

| 本地情境 | 遠端狀態 | 回應驗簽 | 訂單編號 | 金額 |
|---|---:|---|---|---|
| 已付款信用卡 A | `1` | 通過 | 相符 | NT$450 相符 |
| 已付款信用卡 B | `1` | 通過 | 相符 | NT$450 相符 |
| 未付款 ATM | `0` | 通過 | 相符 | NT$450 相符 |

每次結果都寫入 `gateway_query_log`。只有遠端為 `1` 且三項檢查都通過時，
服務才允許將漏掉背景通知的本地訂單補成 `paid`。

### 定期定額實測

```text
建立 recurring checkout（月繳 NT$450，共 3 期）
  → ECPay 付款頁確認週期與金額
  → 官方測試卡＋3D OTP 1234
  → ReturnURL：驗簽通過、金額相符
  → order=paid / subscription=active / progress=1/3
  → QueryCreditCardPeriodInfo：ExecStatus=1
     order、amount、period type、frequency、exec times 全相符
     recovered_charges=1
  → CreditCardPeriodAction(Action=Cancel)
     response signature=passed / RtnCode=1 / RtnMsg=停用成功
  → 再查：ExecStatus=0 / subscription=canceled
```

上述 Query 與 Cancel 的 request／response 都保存 immutable audit log。終止是不可逆
操作；成功後畫面不再提供 Cancel 按鈕。

## 明確限制與非即時可驗證範圍

| 金流商／能力 | 情境 | 前置條件 |
|---|---|---|
| 綠界 ECPay | PeriodReturnURL 第二期通知 | 真實週期尚未到；簽章、金額與冪等已有整合測試 |
| 綠界 ECPay | 對帳檔下載 | 在 vendor-stage 設定執行主機 IP 白名單；CSV import 比對器已完成 |
| 藍新 NewebPay | 信用卡／ATM | 若要補充 live E2E，需建立仍在 30 天效期內的測試商店 |

藍新測試商店憑證自註冊起只有 30 天效期，因此不把憑證寫入 repo，也不以假資料
宣稱 E2E 通過。程式面的 TradeInfo AES-256-CBC、TradeSha、合法／錯誤通知、
冪等與 ATM 兩段式流程已有 contract tests；它不再是本作品 demo 的執行條件。

退款不列為「尚待 E2E」，因為 ECPay 官方明載信用卡請退款 API 無測試端點。
本專案只展示並明確標示 sandbox policy simulation，不會用 production 做測試。
