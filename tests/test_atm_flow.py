"""ATM 兩段式流程：取號（awaiting_payment）→ 入帳（paid）／逾期（expired）。

綠界：PaymentInfoURL 背景通知取號（RtnCode=2）→ 測試後台模擬付款 → ReturnURL 入帳。
藍新：取號無背景通知，唯一來源是 CustomerURL 前景導回（驗簽後僅允許
pending→awaiting_payment）→ 繳費完成才有 NotifyURL。
"""
from sqlmodel import Session, select

from twpay_checkout.gateways.ecpay import generate_check_mac_value
from twpay_checkout.gateways.newebpay import encrypt_trade_info, make_trade_sha
from twpay_checkout.models import Order, OrderStatus

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
from test_notifications import ecpay_payment_payload

PRODUCT_1_PRICE = 450


def ecpay_atm_info_payload(
    order_no: str, amount: int = PRODUCT_1_PRICE, expire_date: str = "2099/12/31", **overrides
) -> dict:
    """ECPay PaymentInfoURL：ATM 取號結果通知（RtnCode=2 為取號成功）。"""
    payload = {
        "MerchantID": ECPAY_MERCHANT_ID,
        "MerchantTradeNo": order_no,
        "TradeNo": f"EC{order_no[1:13]}ATM",
        "RtnCode": "2",
        "RtnMsg": "Get VirtualAccount Succeeded",
        "TradeAmt": str(amount),
        "PaymentType": "ATM_TAISHIN",
        "TradeDate": "2026/07/17 15:00:00",
        "BankCode": "812",
        "vAccount": "9103522175887271",
        "ExpireDate": expire_date,
        **overrides,
    }
    payload["CheckMacValue"] = generate_check_mac_value(payload, ECPAY_HASH_KEY, ECPAY_HASH_IV)
    return payload


def newebpay_vacc_customer_payload(order_no: str, amount: int = PRODUCT_1_PRICE) -> dict:
    """藍新 CustomerURL：VACC 取號完成導回（加密 payload，Status=SUCCESS 表取號成功）。"""
    inner = {
        "Status": "SUCCESS",
        "Message": "取號成功",
        "MerchantID": NEWEBPAY_MERCHANT_ID,
        "Amt": str(amount),
        "TradeNo": f"NP{order_no[1:13]}VAC",
        "MerchantOrderNo": order_no,
        "PaymentType": "VACC",
        "ExpireDate": "2099-12-31",
        "ExpireTime": "23:59:59",
        "BankCode": "808",
        "CodeNo": "9103522175880000",
    }
    cipher = encrypt_trade_info(inner, NEWEBPAY_HASH_KEY, NEWEBPAY_HASH_IV)
    return {
        "Status": "SUCCESS",
        "MerchantID": NEWEBPAY_MERCHANT_ID,
        "Version": "2.0",
        "TradeInfo": cipher,
        "TradeSha": make_trade_sha(cipher, NEWEBPAY_HASH_KEY, NEWEBPAY_HASH_IV),
    }


def newebpay_vacc_paid_payload(order_no: str, amount: int = PRODUCT_1_PRICE) -> dict:
    inner = {
        "Status": "SUCCESS",
        "Message": "付款成功",
        "MerchantID": NEWEBPAY_MERCHANT_ID,
        "Amt": str(amount),
        "TradeNo": f"NP{order_no[1:13]}VAC",
        "MerchantOrderNo": order_no,
        "PaymentType": "VACC",
        "PayTime": "2026-07-17 16:00:00",
        "PayBankCode": "808",
        "PayerAccount5Code": "12345",
    }
    cipher = encrypt_trade_info(inner, NEWEBPAY_HASH_KEY, NEWEBPAY_HASH_IV)
    return {
        "Status": "SUCCESS",
        "MerchantID": NEWEBPAY_MERCHANT_ID,
        "Version": "2.0",
        "TradeInfo": cipher,
        "TradeSha": make_trade_sha(cipher, NEWEBPAY_HASH_KEY, NEWEBPAY_HASH_IV),
    }


# ---- 綠界 ATM ----


def test_ecpay_atm_two_phase_flow(client):
    order_no = place_order(client, "ecpay", "atm")

    # 取號通知（背景）→ awaiting_payment，訂單頁顯示繳費資訊
    response = client.post("/callbacks/ecpay/payment-info", data=ecpay_atm_info_payload(order_no))
    assert response.text == "1|OK"
    assert order_status(client, order_no) == "awaiting_payment"
    detail = client.get(f"/orders/{order_no}").text
    assert "9103522175887271" in detail and "812" in detail

    # 模擬付款入帳（SimulatePaid=1）→ paid
    paid = ecpay_payment_payload(order_no, SimulatePaid="1", PaymentType="ATM_TAISHIN")
    response = client.post("/callbacks/ecpay/notify", data=paid)
    assert response.text == "1|OK"
    assert order_status(client, order_no) == "paid"


def test_ecpay_atm_expired_is_derived_only_and_not_persisted(client, app):
    """expired 是查詢時衍生的顯示狀態：DB 保持 awaiting_payment、不落庫。

    若把 EXPIRED 持久化，逾期後才到的真實入帳通知會被狀態機擋下並回成功，
    造成「錢已收、訂單永遠 expired、金流商不再重送」。
    """
    order_no = place_order(client, "ecpay", "atm")
    client.post(
        "/callbacks/ecpay/payment-info",
        data=ecpay_atm_info_payload(order_no, expire_date="2020/01/01"),
    )

    assert order_status(client, order_no) == "expired"
    with Session(app.state.engine) as session:
        raw = session.exec(select(Order).where(Order.order_no == order_no)).one()
        assert raw.status == OrderStatus.AWAITING_PAYMENT  # DB 未被寫成 EXPIRED


def test_late_payment_after_expiry_still_marks_paid(client):
    """期限前繳費、跨日才銷帳通知的正常情境：以銀行實收為準照樣入帳。"""
    order_no = place_order(client, "ecpay", "atm")
    client.post(
        "/callbacks/ecpay/payment-info",
        data=ecpay_atm_info_payload(order_no, expire_date="2020/01/01"),
    )
    assert order_status(client, order_no) == "expired"  # 頁面已顯示過期

    paid = ecpay_payment_payload(order_no, PaymentType="ATM_TAISHIN")
    response = client.post("/callbacks/ecpay/notify", data=paid)

    assert response.text == "1|OK"
    assert order_status(client, order_no) == "paid"


def test_ecpay_atm_account_issue_amount_is_checked(client):
    """取號通知的金額一樣要比對——不符則不得進 awaiting_payment。"""
    order_no = place_order(client, "ecpay", "atm")
    response = client.post(
        "/callbacks/ecpay/payment-info",
        data=ecpay_atm_info_payload(order_no, amount=999999),
    )

    assert response.text.startswith("0|")
    assert order_status(client, order_no) == "pending"


def test_ecpay_atm_checkout_form_has_atm_fields(client):
    order_no = place_order(client, "ecpay", "atm")
    html = client.get(f"/orders/{order_no}/checkout").text
    assert 'value="ATM"' in html
    assert "PaymentInfoURL" in html and "ClientRedirectURL" in html
    assert "OrderResultURL" not in html  # 官方：非即時付款不支援 OrderResultURL


# ---- 藍新 VACC ----


def test_newebpay_vacc_two_phase_flow(client):
    order_no = place_order(client, "newebpay", "atm")

    # 取號完成導回（前景、驗簽後僅允許 pending→awaiting_payment）
    response = client.post(
        "/callbacks/newebpay/customer",
        data=newebpay_vacc_customer_payload(order_no),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{order_no}"
    assert order_status(client, order_no) == "awaiting_payment"
    detail = client.get(f"/orders/{order_no}").text
    assert "9103522175880000" in detail and "808" in detail

    # 入帳通知（背景）→ paid
    response = client.post("/callbacks/newebpay/notify", data=newebpay_vacc_paid_payload(order_no))
    assert response.status_code == 200
    assert order_status(client, order_no) == "paid"


def test_newebpay_customer_url_never_marks_paid(client):
    """取號導回重複送、甚至在已取號後再送，都不得觸發入帳。"""
    order_no = place_order(client, "newebpay", "atm")
    payload = newebpay_vacc_customer_payload(order_no)

    client.post("/callbacks/newebpay/customer", data=payload)
    client.post("/callbacks/newebpay/customer", data=payload)  # 重複導回

    assert order_status(client, order_no) == "awaiting_payment"  # 不是 paid


def test_newebpay_customer_bad_signature_shows_error_page(client):
    order_no = place_order(client, "newebpay", "atm")
    payload = newebpay_vacc_customer_payload(order_no)
    payload["TradeSha"] = "0" * 64

    response = client.post("/callbacks/newebpay/customer", data=payload)

    assert response.status_code == 400
    assert order_status(client, order_no) == "pending"
