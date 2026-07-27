"""Unified payment-gateway adapter interface shared by ECPay and NewebPay."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from fastapi import Response

from ..models import GatewayName, NotificationKind, Order


def require_gateway_credentials(
    *,
    gateway_label: str,
    merchant_id: str,
    hash_key: str,
    hash_iv: str,
    hash_key_length: int,
    hash_iv_length: int,
) -> None:
    """Fail closed when sandbox credentials are missing or malformed."""
    fields = {
        "MerchantID": merchant_id,
        "HashKey": hash_key,
        "HashIV": hash_iv,
    }
    missing = [name for name, value in fields.items() if not value.strip()]
    if missing:
        raise ValueError(
            f"{gateway_label} sandbox credentials 未設定：{', '.join(missing)}"
        )
    if (
        not hash_key.isascii()
        or not hash_iv.isascii()
        or len(hash_key) != hash_key_length
        or len(hash_iv) != hash_iv_length
    ):
        raise ValueError(
            f"{gateway_label} sandbox credentials 格式錯誤："
            f"HashKey 必須為 {hash_key_length} 個 ASCII 字元，"
            f"HashIV 必須為 {hash_iv_length} 個 ASCII 字元"
        )


class SignatureError(Exception):
    """Notification failed signature verification; carries the raw payload
    so the caller can still record it for auditing (紅線：驗簽失敗要留原始 payload)."""

    def __init__(self, message: str, payload: Mapping[str, str]):
        super().__init__(message)
        self.payload = dict(payload)


@dataclass(frozen=True)
class CallbackUrls:
    """Per-order callback URLs. 兩家對同名參數語意相反，統一用途命名以免混淆。"""

    notify: str  # 背景付款通知（ECPay ReturnURL ／ 藍新 NotifyURL）
    front_return: str  # 前景導回（ECPay OrderResultURL/ClientRedirectURL ／ 藍新 ReturnURL）
    payment_info: str = ""  # ECPay PaymentInfoURL（ATM 取號背景通知）
    customer: str = ""  # 藍新 CustomerURL（取號完成導回）
    period: str = ""  # ECPay PeriodReturnURL（第二期起的定期定額通知）


@dataclass(frozen=True)
class RecurringPlan:
    """ECPay AIO recurring parameters attached to an initial checkout."""

    period_type: str
    frequency: int
    exec_times: int
    period_amount: int


@dataclass(frozen=True)
class PaymentRedirect:
    """Signed form fields for the auto-submit POST to the gateway's hosted page."""

    action_url: str
    fields: dict[str, str]


@dataclass(frozen=True)
class AtmInfo:
    bank_code: str
    virtual_account: str
    pay_deadline: datetime | None


@dataclass(frozen=True)
class ParsedNotification:
    """Gateway-agnostic view of a verified notification."""

    kind: NotificationKind
    order_no: str
    gateway_trade_no: str
    amount: int | None
    success: bool
    message: str = ""
    paid_at: datetime | None = None
    atm: AtmInfo | None = None
    simulated: bool = False  # ECPay SimulatePaid=1（測試後台模擬付款）
    raw: Mapping[str, str] = field(default_factory=dict)


class PaymentGateway(Protocol):
    name: GatewayName

    def create_payment(
        self,
        order: Order,
        product_name: str,
        urls: CallbackUrls,
        recurring: RecurringPlan | None = None,
    ) -> PaymentRedirect:
        """Build the signed form for redirecting the browser to the gateway."""
        ...

    def verify_and_parse_notification(
        self, payload: Mapping[str, str], kind: NotificationKind
    ) -> ParsedNotification:
        """Verify the signature then normalize the payload.

        Raises SignatureError on verification failure. `kind` comes from the
        receiving route: each callback URL only ever carries one kind.
        """
        ...

    def success_response(self) -> Response:
        """官方要求的成功回應（ECPay 純文字 1|OK；藍新 HTTP 200）。"""
        ...

    def failure_response(self, reason: str = "") -> Response:
        """非成功回應，讓金流商重送或留下線索。"""
        ...
