"""ECPay CheckMacValue：以官方文件範例值驗證手寫實作。

範例出處：檢查碼機制 https://developers.ecpay.com.tw/2902/（詳 docs/gateway-notes.md）。
"""
import pytest

from twpay_checkout.gateways.ecpay import (
    generate_check_mac_value,
    net_urlencode,
    verify_check_mac_value,
)

# 官方公開測試商店 3002607 的金鑰（同時是文件範例用的金鑰）
HASH_KEY = "pwFHCqoQZGmho4w6"
HASH_IV = "EkRm7iFT261dpevs"

OFFICIAL_PARAMS = {
    "MerchantID": "3002607",
    "MerchantTradeNo": "ecpay20230312153023",
    "MerchantTradeDate": "2023/03/12 15:30:23",
    "PaymentType": "aio",
    "TotalAmount": "30000",
    "TradeDesc": "促銷方案",
    "ItemName": "Apple iphone 15",
    "ReturnURL": "https://www.ecpay.com.tw/receive.php",
    "ChoosePayment": "ALL",
    "EncryptType": "1",
}
OFFICIAL_CMV = "6C51C9E6888DE861FD62FB1DD17029FC742634498FD813DC43D4243B5685B840"


def test_official_example_generates_expected_check_mac_value():
    assert generate_check_mac_value(OFFICIAL_PARAMS, HASH_KEY, HASH_IV) == OFFICIAL_CMV


def test_verify_accepts_official_example_payload():
    payload = {**OFFICIAL_PARAMS, "CheckMacValue": OFFICIAL_CMV}
    assert verify_check_mac_value(payload, HASH_KEY, HASH_IV)


def test_verify_is_case_insensitive_on_received_mac():
    payload = {**OFFICIAL_PARAMS, "CheckMacValue": OFFICIAL_CMV.lower()}
    assert verify_check_mac_value(payload, HASH_KEY, HASH_IV)


@pytest.mark.parametrize(
    "tamper",
    [
        {"TotalAmount": "1"},  # 金額被竄改
        {"MerchantTradeNo": "ecpay20230312153024"},  # 換單號
        {"CheckMacValue": "0" * 64},  # 假簽章
    ],
)
def test_verify_rejects_tampered_payload(tamper):
    payload = {**OFFICIAL_PARAMS, "CheckMacValue": OFFICIAL_CMV, **tamper}
    assert not verify_check_mac_value(payload, HASH_KEY, HASH_IV)


def test_verify_rejects_missing_mac():
    assert not verify_check_mac_value(OFFICIAL_PARAMS, HASH_KEY, HASH_IV)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 官方轉換表：encode 後須還原的字元
        ("-_.!*()", "-_.!*()"),
        # ~ 必須「保持編碼」%7e（Python quote_plus 預設不編它，最大地雷）
        ("~", "%7e"),
        # 空白轉 +
        ("a b", "a+b"),
        # 其餘符號維持 %xx（轉小寫後）
        ("@#$%^&=+;?/\\<>`[]{}:'\",|", (
            "%40%23%24%25%5e%26%3d%2b%3b%3f%2f%5c%3c%3e%60%5b%5d%7b%7d%3a%27%22%2c%7c"
        )),
        # 中文以 UTF-8 bytes 編碼（官方範例含中文 TradeDesc）
        ("促銷方案", "%e4%bf%83%e9%8a%b7%e6%96%b9%e6%a1%88"),
    ],
)
def test_net_urlencode_matches_official_table(raw, expected):
    assert net_urlencode(raw) == expected


def test_check_mac_sort_is_case_insensitive():
    """通知欄位有小寫開頭者（如 PaymentInfoURL 的 vAccount），排序需不分大小寫。"""
    params = {"vAccount": "123", "BankCode": "007", "MerchantID": "3002607"}
    mac = generate_check_mac_value(params, HASH_KEY, HASH_IV)
    payload = {**params, "CheckMacValue": mac}
    assert verify_check_mac_value(payload, HASH_KEY, HASH_IV)
