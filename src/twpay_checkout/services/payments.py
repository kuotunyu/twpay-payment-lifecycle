"""Order creation and gateway-agnostic notification processing."""
import secrets
import string
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import urlencode

from fastapi import Response
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..gateways.base import CallbackUrls, PaymentGateway, SignatureError
from ..models import (
    GatewayName,
    NotificationKind,
    Order,
    OrderStatus,
    PaymentMethod,
    PaymentNotification,
    ProcessedResult,
    Product,
    Subscription,
    SubscriptionCharge,
    SubscriptionStatus,
)

_SUFFIX_ALPHABET = string.ascii_uppercase + string.digits


def generate_order_no(now: datetime | None = None) -> str:
    """Unique order no shared with both gateways.

    ``T{yyMMddHHmmss}{4 隨機英數}`` = 17 chars — fits ECPay MerchantTradeNo (≤20,
    alnum) and NewebPay MerchantOrderNo (≤30, alnum/underscore); the random
    suffix avoids same-second collisions.
    """
    now = now or datetime.now()
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(4))
    return f"T{now:%y%m%d%H%M%S}{suffix}"


def create_order(
    session: Session,
    product: Product,
    gateway: GatewayName,
    method: PaymentMethod,
) -> Order:
    """Create a pending order; the amount is copied from the DB product price,
    which stays the single source of truth for later amount checks."""
    order = Order(
        order_no=generate_order_no(),
        product_id=product.id,
        amount=product.price,
        status=OrderStatus.PENDING,
        gateway=gateway,
        payment_method=method,
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


def create_subscription(
    session: Session,
    order: Order,
    *,
    period_type: str = "M",
    frequency: int = 1,
    exec_times: int = 3,
) -> Subscription:
    """Attach an ECPay recurring plan to a newly created pending order."""
    if order.id is None:
        raise ValueError("Order must be persisted before creating a subscription")
    subscription = Subscription(
        order_id=order.id,
        period_type=period_type,
        frequency=frequency,
        exec_times=exec_times,
    )
    session.add(subscription)
    session.commit()
    session.refresh(subscription)
    return subscription


def build_callback_urls(base_url: str, gateway: GatewayName) -> CallbackUrls:
    """兩家的回傳網址都逐筆帶在建單參數中（不需改金流後台設定），
    所以 ngrok 換網址只要更新 .env 的 BASE_URL。"""
    base = base_url.rstrip("/")
    if not base:
        raise ValueError(
            "BASE_URL 未設定：請先啟動 tunnel（cloudflared 或 ngrok），"
            "再執行 uv run python scripts/update_tunnel_url.py"
            "（詳見 docs/local-testing.md）"
        )
    prefix = f"{base}/callbacks/{gateway.value}"
    return CallbackUrls(
        notify=f"{prefix}/notify",
        front_return=f"{prefix}/return",
        payment_info=f"{prefix}/payment-info",
        customer=f"{prefix}/customer",
        period=f"{prefix}/period",
    )


def _record(
    session: Session,
    *,
    gateway: GatewayName,
    kind: NotificationKind,
    raw_payload: str,
    signature_valid: bool,
    processed_result: ProcessedResult,
    order_no: str | None = None,
    gateway_trade_no: str | None = None,
    amount_matched: bool | None = None,
    commit: bool = True,
) -> PaymentNotification:
    row = PaymentNotification(
        gateway=gateway,
        order_no=order_no,
        gateway_trade_no=gateway_trade_no,
        kind=kind,
        raw_payload=raw_payload,
        signature_valid=signature_valid,
        amount_matched=amount_matched,
        processed_result=processed_result,
    )
    session.add(row)
    if commit:
        session.commit()
    return row


def process_notification(
    session: Session,
    gateway: PaymentGateway,
    payload: Mapping[str, str],
    kind: NotificationKind,
) -> Response:
    """兩家共用的背景通知處理：驗簽 → 落表 → 比對金額 → 冪等狀態轉移。

    紅線落地：
    - 驗簽失敗：記錄原始 payload、不入帳、回失敗回應（payload 內容不可信，
      order_no/trade_no 一律不採）。
    - 金額不符：記警告（rejected_amount）、不入帳、回失敗回應。
    - 冪等：同一 (gateway, 交易編號, 通知類型) 只會 applied 一次，由部分唯一
      索引在 DB 層保證；重複通知回成功回應以停止金流商重送。
    """
    raw = urlencode(dict(payload))

    try:
        parsed = gateway.verify_and_parse_notification(payload, kind)
    except SignatureError:
        _record(
            session,
            gateway=gateway.name,
            kind=kind,
            raw_payload=raw,
            signature_valid=False,
            processed_result=ProcessedResult.REJECTED_SIGNATURE,
        )
        return gateway.failure_response("SignatureMismatch")

    def record(result: ProcessedResult, *, matched: bool | None = None, commit: bool = True):
        return _record(
            session,
            gateway=gateway.name,
            kind=kind,
            raw_payload=raw,
            signature_valid=True,
            processed_result=result,
            order_no=parsed.order_no,
            gateway_trade_no=parsed.gateway_trade_no,
            amount_matched=matched,
            commit=commit,
        )

    already_applied = session.exec(
        select(PaymentNotification).where(
            PaymentNotification.gateway == gateway.name,
            PaymentNotification.gateway_trade_no == parsed.gateway_trade_no,
            PaymentNotification.kind == kind,
            PaymentNotification.processed_result == ProcessedResult.APPLIED,
        )
    ).first()
    if already_applied is not None:
        record(ProcessedResult.DUPLICATE)
        return gateway.success_response()

    order = session.exec(select(Order).where(Order.order_no == parsed.order_no)).first()
    if order is None:
        record(ProcessedResult.UNKNOWN_ORDER)
        return gateway.failure_response("UnknownOrder")

    if parsed.amount is None or parsed.amount != order.amount:
        record(ProcessedResult.REJECTED_AMOUNT, matched=False)
        return gateway.failure_response("AmountMismatch")

    applied = _apply_transition(order, parsed, kind)
    if not applied:
        # 通知本身合法但目前狀態不允許此轉移（例：已付款訂單又收到失敗通知）。
        # 留稽核紀錄並回成功以停止重送。
        record(ProcessedResult.IGNORED_TRANSITION, matched=True)
        return gateway.success_response()

    try:
        # 訂單狀態與 applied 紀錄在同一交易內寫入；撞到部分唯一索引代表
        # 另一次重複通知已搶先入帳。
        session.add(order)
        if (
            kind == NotificationKind.PAYMENT_RESULT
            and parsed.success
            and order.id is not None
        ):
            subscription = session.exec(
                select(Subscription).where(Subscription.order_id == order.id)
            ).first()
            if subscription is not None:
                subscription.status = SubscriptionStatus.ACTIVE
                subscription.success_times = max(subscription.success_times, 1)
                subscription.success_amount = max(
                    subscription.success_amount, order.amount
                )
                subscription.last_trade_no = parsed.gateway_trade_no
                subscription.last_message = parsed.message
                subscription.last_charged_at = parsed.paid_at or datetime.now()
                subscription.updated_at = datetime.now()
                session.add(subscription)
        record(ProcessedResult.APPLIED, matched=True, commit=False)
        session.commit()
    except IntegrityError:
        session.rollback()
        record(ProcessedResult.DUPLICATE, matched=True)
        return gateway.success_response()
    return gateway.success_response()


def process_recurring_notification(
    session: Session,
    gateway: PaymentGateway,
    payload: Mapping[str, str],
) -> Response:
    """Verify and apply an ECPay PeriodReturnURL notification idempotently."""
    kind = NotificationKind.RECURRING_PAYMENT
    raw = urlencode(dict(payload))
    try:
        parsed = gateway.verify_and_parse_notification(payload, kind)
    except SignatureError:
        _record(
            session,
            gateway=gateway.name,
            kind=kind,
            raw_payload=raw,
            signature_valid=False,
            processed_result=ProcessedResult.REJECTED_SIGNATURE,
        )
        return gateway.failure_response("SignatureMismatch")

    def record(
        result: ProcessedResult,
        *,
        matched: bool | None = None,
        commit: bool = True,
    ) -> PaymentNotification:
        return _record(
            session,
            gateway=gateway.name,
            kind=kind,
            raw_payload=raw,
            signature_valid=True,
            processed_result=result,
            order_no=parsed.order_no,
            gateway_trade_no=parsed.gateway_trade_no,
            amount_matched=matched,
            commit=commit,
        )

    already_applied = session.exec(
        select(PaymentNotification).where(
            PaymentNotification.gateway == gateway.name,
            PaymentNotification.gateway_trade_no == parsed.gateway_trade_no,
            PaymentNotification.kind == kind,
            PaymentNotification.processed_result == ProcessedResult.APPLIED,
        )
    ).first()
    if already_applied is not None:
        record(ProcessedResult.DUPLICATE)
        return gateway.success_response()

    order = session.exec(select(Order).where(Order.order_no == parsed.order_no)).first()
    if order is None or order.id is None:
        record(ProcessedResult.UNKNOWN_ORDER)
        return gateway.failure_response("UnknownOrder")
    subscription = session.exec(
        select(Subscription).where(Subscription.order_id == order.id)
    ).first()
    if subscription is None:
        record(ProcessedResult.UNKNOWN_ORDER)
        return gateway.failure_response("UnknownSubscription")
    if parsed.amount is None or parsed.amount != order.amount:
        record(ProcessedResult.REJECTED_AMOUNT, matched=False)
        return gateway.failure_response("AmountMismatch")

    total_times_text = payload.get("TotalSuccessTimes", "")
    total_times = (
        int(total_times_text)
        if total_times_text.isdigit()
        else subscription.success_times + (1 if parsed.success else 0)
    )
    total_amount_text = payload.get("TotalSuccessAmount", "")
    total_amount = (
        int(total_amount_text)
        if total_amount_text.isdigit()
        else subscription.success_amount + (order.amount if parsed.success else 0)
    )
    trade_no = parsed.gateway_trade_no or (
        f"{parsed.order_no}-P{total_times}-{payload.get('RtnCode', '')}"
    )
    charge = SubscriptionCharge(
        subscription_id=subscription.id,
        gateway_trade_no=trade_no,
        sequence=max(total_times, 1),
        amount=parsed.amount,
        success=parsed.success,
        amount_matched=True,
        message=parsed.message,
        raw_payload=raw,
        charged_at=parsed.paid_at or datetime.now(),
    )
    if parsed.success:
        subscription.success_times = max(subscription.success_times, total_times)
        subscription.success_amount = max(subscription.success_amount, total_amount)
        subscription.status = (
            SubscriptionStatus.COMPLETED
            if subscription.success_times >= subscription.exec_times
            else SubscriptionStatus.ACTIVE
        )
    else:
        subscription.failure_times += 1
        subscription.status = SubscriptionStatus.PAST_DUE
    subscription.last_trade_no = trade_no
    subscription.last_message = parsed.message
    subscription.last_charged_at = parsed.paid_at or datetime.now()
    subscription.updated_at = datetime.now()

    try:
        session.add(subscription)
        session.add(charge)
        record(ProcessedResult.APPLIED, matched=True, commit=False)
        session.commit()
    except IntegrityError:
        session.rollback()
        record(ProcessedResult.DUPLICATE, matched=True)
    return gateway.success_response()


def _apply_transition(order: Order, parsed, kind: NotificationKind) -> bool:
    """套用狀態機允許的轉移；回傳是否有套用。"""
    if kind == NotificationKind.ATM_ACCOUNT_ISSUED:
        if parsed.success and parsed.atm and order.status == OrderStatus.PENDING:
            order.status = OrderStatus.AWAITING_PAYMENT
            order.gateway_trade_no = parsed.gateway_trade_no
            order.bank_code = parsed.atm.bank_code
            order.virtual_account = parsed.atm.virtual_account
            order.pay_deadline = parsed.atm.pay_deadline
            return True
        return False

    if parsed.success and order.status in (
        OrderStatus.PENDING,
        OrderStatus.AWAITING_PAYMENT,
        OrderStatus.EXPIRED,  # 逾期後才銷帳的真實付款：以銀行實收為準照樣入帳
    ):
        order.gateway_trade_no = parsed.gateway_trade_no
        order.status = OrderStatus.PAID
        order.paid_at = parsed.paid_at or datetime.now()
        return True
    if not parsed.success and order.status in (
        OrderStatus.PENDING,
        OrderStatus.AWAITING_PAYMENT,
    ):
        order.gateway_trade_no = parsed.gateway_trade_no
        order.status = OrderStatus.FAILED
        return True
    return False


def effective_status(order: Order) -> OrderStatus:
    """Lazy expiry: an awaiting-payment order past its pay deadline is expired.

    Neither gateway sends a notification for unpaid expired ATM orders, so the
    state is derived at read time instead of by a background job.
    """
    if (
        order.status == OrderStatus.AWAITING_PAYMENT
        and order.pay_deadline is not None
        and datetime.now() > order.pay_deadline
    ):
        return OrderStatus.EXPIRED
    return order.status
