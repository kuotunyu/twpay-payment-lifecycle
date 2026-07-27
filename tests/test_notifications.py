"""整合測試：模擬金流商背景通知的安全處理。

情境（plan.md 測試計畫）：合法通知、金額被竄改、簽章錯誤、重複通知、
付款失敗、查無訂單。payload 由自家簽章函式產生——該函式已用官方範例值釘死。
"""
from urllib.parse import quote_plus

from twpay_checkout.gateways.ecpay import generate_check_mac_value
from twpay_checkout.gateways.newebpay import encrypt_trade_info, make_trade_sha

from conftest import (
    ECPAY_HASH_IV,
    ECPAY_HASH_KEY,
    ECPAY_MERCHANT_ID,
    NEWEBPAY_HASH_IV,
    NEWEBPAY_HASH_KEY,
    NEWEBPAY_MERCHANT_ID,
    order_status,
    place_order,
)

PRODUCT_1_PRICE = 450  # db.SEED_PRODUCTS[0]


def ecpay_payment_payload(order_no: str, amount: int = PRODUCT_1_PRICE, **overrides) -> dict:
    """組出一筆簽章正確的 ECPay 付款結果通知（ReturnURL 格式）。"""
    payload = {
        "MerchantID": ECPAY_MERCHANT_ID,
        "MerchantTradeNo": order_no,
        "TradeNo": f"EC{order_no[1:13]}001",
        "RtnCode": "1",
        "RtnMsg": "交易成功",
        "TradeAmt": str(amount),
        "PaymentDate": "2026/07/17 15:00:00",
        "PaymentType": "Credit_CreditCard",
        "TradeDate": "2026/07/17 14:59:00",
        "SimulatePaid": "0",
        **overrides,
    }
    payload["CheckMacValue"] = generate_check_mac_value(
        payload, ECPAY_HASH_KEY, ECPAY_HASH_IV
    )
    return payload


def audit_rows(client, order_no: str | None = None) -> list[str]:
    """從稽核頁抓處理結果欄（粗略但足夠驗證落表行為）。"""
    html = client.get("/admin/notifications").text
    return [
        result
        for result in (
            "applied", "duplicate", "rejected_signature",
            "rejected_amount", "unknown_order", "ignored_transition",
        )
        if f"<strong>{result}</strong>" in html
    ]


def test_legit_payment_notification_marks_order_paid(client):
    order_no = place_order(client, "ecpay", "credit_card")
    response = client.post("/callbacks/ecpay/notify", data=ecpay_payment_payload(order_no))

    assert response.text == "1|OK"  # 官方要求的成功回應，一字不差
    assert order_status(client, order_no) == "paid"
    assert "applied" in audit_rows(client)


def test_ecpay_raw_utf8_form_value_keeps_signature_valid(client):
    """ECPay live callbacks may send raw UTF-8 instead of percent-encoded Chinese.

    Starlette decodes those bytes as Latin-1. The callback boundary must repair
    the value before CheckMacValue verification.
    """
    order_no = place_order(client, "ecpay", "credit_card")
    payload = ecpay_payment_payload(order_no)
    body_parts: list[bytes] = []
    for key, value in payload.items():
        encoded_value = (
            value.encode("utf-8")
            if key == "RtnMsg"
            else quote_plus(value).encode("ascii")
        )
        body_parts.append(key.encode("ascii") + b"=" + encoded_value)

    response = client.post(
        "/callbacks/ecpay/notify",
        content=b"&".join(body_parts),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.text == "1|OK"
    assert order_status(client, order_no) == "paid"
    assert "applied" in audit_rows(client)


def test_tampered_amount_is_rejected(client):
    """簽章正確但金額與訂單不符 → 記警告、拒絕入帳。"""
    order_no = place_order(client, "ecpay", "credit_card")
    response = client.post(
        "/callbacks/ecpay/notify",
        data=ecpay_payment_payload(order_no, amount=1),
    )

    assert response.text != "1|OK" and response.text.startswith("0|")
    assert order_status(client, order_no) == "pending"
    assert "rejected_amount" in audit_rows(client)
    assert "applied" not in audit_rows(client)


def test_bad_signature_is_rejected_and_recorded(client):
    order_no = place_order(client, "ecpay", "credit_card")
    payload = ecpay_payment_payload(order_no)
    payload["CheckMacValue"] = "F" * 64

    response = client.post("/callbacks/ecpay/notify", data=payload)

    assert response.text.startswith("0|")
    assert order_status(client, order_no) == "pending"
    assert "rejected_signature" in audit_rows(client)
    # 原始 payload 必須留存供稽核
    assert order_no in client.get("/admin/notifications").text


def test_duplicate_notification_is_idempotent(client):
    order_no = place_order(client, "ecpay", "credit_card")
    payload = ecpay_payment_payload(order_no)

    first = client.post("/callbacks/ecpay/notify", data=payload)
    second = client.post("/callbacks/ecpay/notify", data=payload)

    assert first.text == "1|OK"
    assert second.text == "1|OK"  # 重複通知也回成功，停止金流商重送
    assert order_status(client, order_no) == "paid"
    html = client.get("/admin/notifications").text
    assert html.count("<strong>applied</strong>") == 1  # 只入帳一次
    assert html.count("<strong>duplicate</strong>") == 1


def test_failed_payment_marks_order_failed(client):
    order_no = place_order(client, "ecpay", "credit_card")
    payload = ecpay_payment_payload(order_no, RtnCode="10200095", RtnMsg="交易失敗")

    response = client.post("/callbacks/ecpay/notify", data=payload)

    assert response.text == "1|OK"
    assert order_status(client, order_no) == "failed"


def test_unknown_order_is_rejected(client):
    payload = ecpay_payment_payload("T9901010000009999")
    response = client.post("/callbacks/ecpay/notify", data=payload)

    assert response.text.startswith("0|")
    assert "unknown_order" in audit_rows(client)


def test_failure_notification_after_paid_is_ignored(client):
    """已付款訂單再收到失敗通知 → 不得倒退狀態（非法轉移只記錄）。"""
    order_no = place_order(client, "ecpay", "credit_card")
    client.post("/callbacks/ecpay/notify", data=ecpay_payment_payload(order_no))

    late_failure = ecpay_payment_payload(
        order_no, RtnCode="10200095", TradeNo="EC000000000000999"
    )
    response = client.post("/callbacks/ecpay/notify", data=late_failure)

    assert response.text == "1|OK"
    assert order_status(client, order_no) == "paid"
    assert "ignored_transition" in audit_rows(client)


def test_front_return_is_display_only(client):
    """前景導回不改狀態：導回後訂單仍是 pending（狀態只信背景通知）。"""
    order_no = place_order(client, "ecpay", "credit_card")
    response = client.post(
        "/callbacks/ecpay/return",
        data={"MerchantTradeNo": order_no, "RtnCode": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{order_no}"
    assert order_status(client, order_no) == "pending"


# ---- 藍新 NewebPay ----


def newebpay_notify_payload(
    order_no: str, amount: int = PRODUCT_1_PRICE, status: str = "SUCCESS", **inner_overrides
) -> dict:
    """組出一筆 TradeSha 正確的藍新付款結果通知（NotifyURL 格式）。"""
    inner = {
        "Status": status,
        "Message": "授權成功" if status == "SUCCESS" else "授權失敗",
        "MerchantID": NEWEBPAY_MERCHANT_ID,
        "Amt": str(amount),
        "TradeNo": f"NP{order_no[1:13]}001",
        "MerchantOrderNo": order_no,
        "RespondType": "String",
        "PaymentType": "CREDIT",
        "PayTime": "2026-07-17 15:00:00",
        **inner_overrides,
    }
    cipher = encrypt_trade_info(inner, NEWEBPAY_HASH_KEY, NEWEBPAY_HASH_IV)
    return {
        "Status": status,
        "MerchantID": NEWEBPAY_MERCHANT_ID,
        "Version": "2.0",
        "TradeInfo": cipher,
        "TradeSha": make_trade_sha(cipher, NEWEBPAY_HASH_KEY, NEWEBPAY_HASH_IV),
    }


def test_newebpay_legit_notification_marks_order_paid(client):
    order_no = place_order(client, "newebpay", "credit_card")
    response = client.post("/callbacks/newebpay/notify", data=newebpay_notify_payload(order_no))

    assert response.status_code == 200  # 官方只認 HTTP 200
    assert order_status(client, order_no) == "paid"


def test_newebpay_tampered_amount_is_rejected(client):
    order_no = place_order(client, "newebpay", "credit_card")
    response = client.post(
        "/callbacks/newebpay/notify",
        data=newebpay_notify_payload(order_no, amount=1),
    )

    assert response.status_code != 200
    assert order_status(client, order_no) == "pending"
    assert "rejected_amount" in audit_rows(client)


def test_newebpay_bad_trade_sha_is_rejected(client):
    order_no = place_order(client, "newebpay", "credit_card")
    payload = newebpay_notify_payload(order_no)
    payload["TradeSha"] = "F" * 64

    response = client.post("/callbacks/newebpay/notify", data=payload)

    assert response.status_code != 200
    assert order_status(client, order_no) == "pending"
    assert "rejected_signature" in audit_rows(client)


def test_newebpay_duplicate_notification_is_idempotent(client):
    order_no = place_order(client, "newebpay", "credit_card")
    payload = newebpay_notify_payload(order_no)

    first = client.post("/callbacks/newebpay/notify", data=payload)
    second = client.post("/callbacks/newebpay/notify", data=payload)

    assert first.status_code == 200 and second.status_code == 200
    assert order_status(client, order_no) == "paid"
    html = client.get("/admin/notifications").text
    assert html.count("<strong>applied</strong>") == 1


def test_newebpay_failed_payment_marks_order_failed(client):
    order_no = place_order(client, "newebpay", "credit_card")
    payload = newebpay_notify_payload(order_no, status="FAIL", TradeNo="NP000000000000002")

    response = client.post("/callbacks/newebpay/notify", data=payload)

    assert response.status_code == 200
    assert order_status(client, order_no) == "failed"


def test_newebpay_front_return_is_display_only(client):
    order_no = place_order(client, "newebpay", "credit_card")
    response = client.post(
        "/callbacks/newebpay/return",
        data=newebpay_notify_payload(order_no),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{order_no}"
    assert order_status(client, order_no) == "pending"  # 前景導回不入帳


# ---- DB 層冪等防線（部分唯一索引本體）----


def test_applied_unique_index_is_enforced_at_db_level(client, app):
    """直接對 DB 插入第二筆同鍵 applied 列必須被唯一索引擋下。

    回歸保護：SQLAlchemy 對 str-Enum 預設以「名稱」落庫，若索引 WHERE
    條件的字面值與落庫值不一致，索引會涵蓋 0 列、防線整個失效——這支
    測試繞過 app 層預檢，釘死索引本身有效。
    """
    import pytest
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session

    from twpay_checkout.models import (
        GatewayName,
        NotificationKind,
        PaymentNotification,
        ProcessedResult,
    )

    def make_row(result: ProcessedResult) -> PaymentNotification:
        return PaymentNotification(
            gateway=GatewayName.ECPAY,
            order_no="T0000000000000000",
            gateway_trade_no="EC_DUP_TEST",
            kind=NotificationKind.PAYMENT_RESULT,
            raw_payload="raw",
            signature_valid=True,
            processed_result=result,
        )

    with Session(app.state.engine) as session:
        session.add(make_row(ProcessedResult.APPLIED))
        session.commit()
        # 非 applied 的稽核列不受唯一索引限制（同鍵可多列）
        session.add(make_row(ProcessedResult.DUPLICATE))
        session.commit()
        # 第二筆 applied 必須撞索引
        session.add(make_row(ProcessedResult.APPLIED))
        with pytest.raises(IntegrityError):
            session.commit()


# ---- checkout 表單（兩家都只指向測試環境端點）----


def test_ecpay_checkout_form_targets_stage_only(client):
    order_no = place_order(client, "ecpay", "credit_card")
    html = client.get(f"/orders/{order_no}/checkout").text
    assert "https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5" in html
    assert "payment.ecpay.com.tw/Cashier" not in html.replace("payment-stage.ecpay.com.tw", "")
    assert "CheckMacValue" in html


def test_newebpay_checkout_form_targets_stage_only(client):
    order_no = place_order(client, "newebpay", "credit_card")
    html = client.get(f"/orders/{order_no}/checkout").text
    assert "https://ccore.newebpay.com/MPG/mpg_gateway" in html
    assert "TradeInfo" in html and "TradeSha" in html
    # 交易參數已整包加密進 TradeInfo，頁面不得出現明文回呼網址
    assert "callbacks/newebpay/notify" not in html
