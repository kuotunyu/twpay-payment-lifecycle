"""綠界 ECPay 全方位金流（AIO V5）adapter，CheckMacValue 依官方演算法手寫。

規格來源（詳見 docs/gateway-notes.md）：
- 檢查碼機制：https://developers.ecpay.com.tw/2902/
- URLENCODE 轉換表：https://developers.ecpay.com.tw/?p=7446
- 產生訂單：https://developers.ecpay.com.tw/?p=2862
"""
import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, quote_plus, urlsplit

from fastapi import Response
from fastapi.responses import PlainTextResponse

from ..config import Settings
from ..models import GatewayName, NotificationKind, Order, PaymentMethod
from .base import (
    AtmInfo,
    CallbackUrls,
    ParsedNotification,
    PaymentRedirect,
    RecurringPlan,
    SignatureError,
)

# 紅線：只允許測試環境端點，正式環境一律禁止。
STAGE_CHECKOUT_URL = "https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5"
STAGE_QUERY_URL = "https://payment-stage.ecpay.com.tw/Cashier/QueryTradeInfo/V5"
STAGE_PERIOD_QUERY_URL = (
    "https://payment-stage.ecpay.com.tw/Cashier/QueryCreditCardPeriodInfo"
)
STAGE_PERIOD_ACTION_URL = (
    "https://payment-stage.ecpay.com.tw/Cashier/CreditCardPeriodAction"
)
_ALLOWED_TEST_HOST = "payment-stage.ecpay.com.tw"

ATM_EXPIRE_DAYS = 3  # ExpireDate 單位「天」，官方預設 3、範圍 1–60


@dataclass(frozen=True)
class EcpayQueryResult:
    """Verified view of a QueryTradeInfo/V5 response."""

    order_no: str
    gateway_trade_no: str
    trade_status: str
    amount: int | None
    payment_date: datetime | None
    signature_valid: bool
    raw: dict[str, str]


@dataclass(frozen=True)
class EcpayPeriodActionResult:
    order_no: str
    code: str
    message: str
    signature_valid: bool
    raw: dict[str, str]


@dataclass(frozen=True)
class EcpayPeriodQueryResult:
    merchant_id: str
    order_no: str
    code: str
    period_type: str
    frequency: int | None
    exec_times: int | None
    period_amount: int | None
    total_success_times: int | None
    total_success_amount: int | None
    exec_status: str
    exec_log: tuple[dict[str, object], ...]
    message: str
    raw: dict[str, object]


def _assert_stage(url: str) -> str:
    host = urlsplit(url).hostname or ""
    if host != _ALLOWED_TEST_HOST:
        raise AssertionError(f"Refusing non-stage ECPay endpoint: {url}")
    return url


def net_urlencode(text: str) -> str:
    """官方 URLENCODE 轉換表「.NET 編碼(ecpay)」風格編碼，輸出小寫。

    與 Python quote_plus 的差異（p=7446）：
    - ``! * ( )`` encode 後必須還原成原字元（``- _ .`` quote_plus 本就不編碼）
    - ``~`` quote_plus 不編碼，但 ECPay 要求保持 ``%7e``
    - 空白轉 ``+``（quote_plus 已符合）
    """
    encoded = quote_plus(text, encoding="utf-8").lower()
    for escaped, plain in (("%21", "!"), ("%2a", "*"), ("%28", "("), ("%29", ")")):
        encoded = encoded.replace(escaped, plain)
    return encoded.replace("~", "%7e")


def generate_check_mac_value(
    params: Mapping[str, str], hash_key: str, hash_iv: str
) -> str:
    """依官方演算法產生 CheckMacValue（EncryptType=1 / SHA256）。

    步驟：排除 CheckMacValue → 參數名 A–Z 排序（不分大小寫，同官方 SDK 行為，
    通知欄位有 vAccount 這種小寫開頭）→ 前後串 HashKey/HashIV →
    .NET 風格 URL encode → 轉小寫 → SHA256 → 轉大寫。
    """
    filtered = {k: v for k, v in params.items() if k != "CheckMacValue"}
    joined = "&".join(f"{k}={filtered[k]}" for k in sorted(filtered, key=str.lower))
    raw = f"HashKey={hash_key}&{joined}&HashIV={hash_iv}"
    return hashlib.sha256(net_urlencode(raw).encode("utf-8")).hexdigest().upper()


def verify_check_mac_value(
    payload: Mapping[str, str], hash_key: str, hash_iv: str
) -> bool:
    """驗證回傳通知的 CheckMacValue（大小寫不敏感比對前先統一大寫）。"""
    received = payload.get("CheckMacValue", "")
    if not received:
        return False
    expected = generate_check_mac_value(payload, hash_key, hash_iv)
    return received.upper() == expected


def _sanitize_text(name: str, limit: int) -> str:
    """ECPay 參數不支援特殊符號；只留中英數、空白與連字號。"""
    return re.sub(r"[^0-9A-Za-z一-鿿\s-]", "", name).strip()[:limit] or "item"


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def _optional_int(value: object) -> int | None:
    text = str(value)
    return int(text) if text.isdigit() else None


def validate_recurring_plan(plan: RecurringPlan) -> None:
    """Reject recurring settings outside ECPay's documented AIO ranges."""
    limits = {"D": (1, 365), "M": (1, 12), "Y": (1, 1)}
    if plan.period_type not in limits:
        raise ValueError("PeriodType must be D, M, or Y")
    low, high = limits[plan.period_type]
    if not low <= plan.frequency <= high:
        raise ValueError(
            f"Frequency for {plan.period_type} must be between {low} and {high}"
        )
    if plan.exec_times < 2:
        raise ValueError("ExecTimes must be at least 2")
    if plan.period_amount <= 0:
        raise ValueError("PeriodAmount must be positive")


class EcpayGateway:
    name = GatewayName.ECPAY

    def __init__(self, settings: Settings):
        self.merchant_id = settings.ecpay_merchant_id
        self.hash_key = settings.ecpay_hash_key
        self.hash_iv = settings.ecpay_hash_iv
        self.checkout_url = _assert_stage(STAGE_CHECKOUT_URL)
        self.query_url = _assert_stage(STAGE_QUERY_URL)
        self.period_query_url = _assert_stage(STAGE_PERIOD_QUERY_URL)
        self.period_action_url = _assert_stage(STAGE_PERIOD_ACTION_URL)

    def create_payment(
        self,
        order: Order,
        product_name: str,
        urls: CallbackUrls,
        recurring: RecurringPlan | None = None,
    ) -> PaymentRedirect:
        params: dict[str, str] = {
            "MerchantID": self.merchant_id,
            "MerchantTradeNo": order.order_no,
            "MerchantTradeDate": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
            "PaymentType": "aio",
            "TotalAmount": str(order.amount),  # 金額以 DB 訂單為唯一真相
            "TradeDesc": "twpay-checkout demo",
            "ItemName": _sanitize_text(product_name, 400),
            "ReturnURL": urls.notify,  # ECPay 的 ReturnURL 是「背景」通知
            "EncryptType": "1",
        }
        if order.payment_method == PaymentMethod.CREDIT_CARD:
            params["ChoosePayment"] = "Credit"
            params["OrderResultURL"] = urls.front_return  # 前景導回（POST，不支援 ATM）
            if recurring is not None:
                validate_recurring_plan(recurring)
                if recurring.period_amount != order.amount:
                    raise ValueError("PeriodAmount must equal the DB order amount")
                if not urls.period:
                    raise ValueError("PeriodReturnURL is required for recurring payments")
                params.update(
                    {
                        "PeriodAmount": str(recurring.period_amount),
                        "PeriodType": recurring.period_type,
                        "Frequency": str(recurring.frequency),
                        "ExecTimes": str(recurring.exec_times),
                        "PeriodReturnURL": urls.period,
                    }
                )
        else:
            if recurring is not None:
                raise ValueError("Recurring payments require a credit card")
            params["ChoosePayment"] = "ATM"
            params["ExpireDate"] = str(ATM_EXPIRE_DAYS)
            params["PaymentInfoURL"] = urls.payment_info  # ATM 取號結果背景通知
            params["ClientRedirectURL"] = urls.front_return  # ATM 取號後前景導回
        params["CheckMacValue"] = generate_check_mac_value(
            params, self.hash_key, self.hash_iv
        )
        return PaymentRedirect(action_url=self.checkout_url, fields=params)

    def build_query_request(
        self, order_no: str, timestamp: int | None = None
    ) -> PaymentRedirect:
        """Build a signed server-to-server QueryTradeInfo/V5 form request."""
        params = {
            "MerchantID": self.merchant_id,
            "MerchantTradeNo": order_no,
            "TimeStamp": str(timestamp if timestamp is not None else int(time.time())),
        }
        params["CheckMacValue"] = generate_check_mac_value(
            params, self.hash_key, self.hash_iv
        )
        return PaymentRedirect(action_url=self.query_url, fields=params)

    def parse_query_response(self, body: str) -> EcpayQueryResult:
        """Parse and verify ECPay's URL-encoded QueryTradeInfo response."""
        payload = dict(parse_qsl(body, keep_blank_values=True, encoding="utf-8"))
        amount_text = payload.get("TradeAmt", "")
        return EcpayQueryResult(
            order_no=payload.get("MerchantTradeNo", ""),
            gateway_trade_no=payload.get("TradeNo", ""),
            trade_status=payload.get("TradeStatus", ""),
            amount=int(amount_text) if amount_text.isdigit() else None,
            payment_date=_parse_datetime(payload.get("PaymentDate", "")),
            signature_valid=verify_check_mac_value(
                payload, self.hash_key, self.hash_iv
            ),
            raw=payload,
        )

    def build_period_action_request(
        self,
        order_no: str,
        *,
        action: str,
        timestamp: int | None = None,
    ) -> PaymentRedirect:
        """Build a signed recurring Cancel/ReAuth request for the stage API."""
        if action not in {"Cancel", "ReAuth"}:
            raise ValueError("Recurring action must be Cancel or ReAuth")
        params = {
            "MerchantID": self.merchant_id,
            "MerchantTradeNo": order_no,
            "Action": action,
            "TimeStamp": str(timestamp if timestamp is not None else int(time.time())),
        }
        params["CheckMacValue"] = generate_check_mac_value(
            params, self.hash_key, self.hash_iv
        )
        return PaymentRedirect(action_url=self.period_action_url, fields=params)

    def build_period_query_request(
        self, order_no: str, timestamp: int | None = None
    ) -> PaymentRedirect:
        """Build a signed QueryCreditCardPeriodInfo stage request."""
        params = {
            "MerchantID": self.merchant_id,
            "MerchantTradeNo": order_no,
            "TimeStamp": str(timestamp if timestamp is not None else int(time.time())),
        }
        params["CheckMacValue"] = generate_check_mac_value(
            params, self.hash_key, self.hash_iv
        )
        return PaymentRedirect(action_url=self.period_query_url, fields=params)

    def parse_period_query_response(self, body: str) -> EcpayPeriodQueryResult:
        """Parse QueryCreditCardPeriodInfo's documented JSON response."""
        parsed = json.loads(body.lstrip("\ufeff"))
        if not isinstance(parsed, dict):
            raise ValueError("ECPay period query response must be a JSON object")
        raw: dict[str, object] = {str(key): value for key, value in parsed.items()}
        log_value = raw.get("ExecLog", [])
        exec_log = (
            tuple(
                {str(key): value for key, value in item.items()}
                for item in log_value
                if isinstance(item, dict)
            )
            if isinstance(log_value, list)
            else ()
        )
        return EcpayPeriodQueryResult(
            merchant_id=str(raw.get("MerchantID", "")),
            order_no=str(raw.get("MerchantTradeNo", "")),
            code=str(raw.get("RtnCode", "")),
            period_type=str(raw.get("PeriodType", "")),
            frequency=_optional_int(raw.get("Frequency", "")),
            exec_times=_optional_int(raw.get("ExecTimes", "")),
            period_amount=_optional_int(raw.get("PeriodAmount", "")),
            total_success_times=_optional_int(raw.get("TotalSuccessTimes", "")),
            total_success_amount=_optional_int(raw.get("TotalSuccessAmount", "")),
            exec_status=str(raw.get("ExecStatus", "")),
            exec_log=exec_log,
            message=str(raw.get("RtnMsg", "")),
            raw=raw,
        )

    def parse_period_action_response(self, body: str) -> EcpayPeriodActionResult:
        """Parse and verify CreditCardPeriodAction's URL-encoded response."""
        payload = dict(parse_qsl(body, keep_blank_values=True, encoding="utf-8"))
        return EcpayPeriodActionResult(
            order_no=payload.get("MerchantTradeNo", ""),
            code=payload.get("RtnCode", ""),
            message=payload.get("RtnMsg", ""),
            signature_valid=verify_check_mac_value(
                payload, self.hash_key, self.hash_iv
            ),
            raw=payload,
        )

    def verify_and_parse_notification(
        self, payload: Mapping[str, str], kind: NotificationKind
    ) -> ParsedNotification:
        if not verify_check_mac_value(payload, self.hash_key, self.hash_iv):
            raise SignatureError("ECPay CheckMacValue mismatch", payload)

        rtn_code = payload.get("RtnCode", "")
        amount_key = "Amount" if kind == NotificationKind.RECURRING_PAYMENT else "TradeAmt"
        amount_text = payload.get(amount_key, "")
        amount = int(amount_text) if amount_text.isdigit() else None
        atm: AtmInfo | None = None
        paid_at: datetime | None = None

        if kind == NotificationKind.ATM_ACCOUNT_ISSUED:
            success = rtn_code == "2"  # ATM 取號成功的 RtnCode=2（PaymentInfoURL）
            if success:
                deadline = None
                expire = payload.get("ExpireDate", "")
                try:
                    day = datetime.strptime(expire, "%Y/%m/%d")
                    deadline = day.replace(hour=23, minute=59, second=59)
                except ValueError:
                    pass
                atm = AtmInfo(
                    bank_code=payload.get("BankCode", ""),
                    virtual_account=payload.get("vAccount", ""),
                    pay_deadline=deadline,
                )
        else:
            success = rtn_code == "1"  # 付款成功通知唯一成功值
            if success:
                paid_at = _parse_datetime(
                    payload.get("PaymentDate", "")
                    or payload.get("ProcessDate", "")
                    or payload.get("process_date", "")
                )

        return ParsedNotification(
            kind=kind,
            order_no=payload.get("MerchantTradeNo", ""),
            gateway_trade_no=payload.get("TradeNo", ""),
            amount=amount,
            success=success,
            message=payload.get("RtnMsg", ""),
            paid_at=paid_at,
            atm=atm,
            simulated=payload.get("SimulatePaid") == "1",
            raw=dict(payload),
        )

    def success_response(self) -> Response:
        # 官方：純文字 1|OK；帶引號、小寫、多餘空白都視為未回應而重送
        return PlainTextResponse("1|OK")

    def failure_response(self, reason: str = "") -> Response:
        # 官方未定義失敗字串；只要非 1|OK 即會於 5–15 分鐘後重送（當天最多 4 次）
        return PlainTextResponse(f"0|{reason or 'Error'}")
