"""ECPay payment-operations services: query recovery, refunds, and reconciliation."""
from __future__ import annotations

import csv
import io
import secrets
from collections.abc import Callable, Mapping
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from sqlmodel import Session, select

from ..gateways.base import PaymentRedirect
from ..gateways.ecpay import EcpayGateway, generate_check_mac_value
from ..models import (
    GatewayName,
    GatewayQueryLog,
    Order,
    OrderStatus,
    PaymentMethod,
    QueryOutcome,
    ReconciliationItem,
    ReconciliationResult,
    ReconciliationRun,
    ReconciliationStatus,
    RefundAction,
    RefundRequest,
    RefundStatus,
    Subscription,
    SubscriptionAction,
    SubscriptionActionLog,
    SubscriptionActionStatus,
    SubscriptionCharge,
    SubscriptionQueryLog,
    SubscriptionQueryOutcome,
    SubscriptionStatus,
)

FormTransport = Callable[[str, Mapping[str, str]], str]

STAGE_RECONCILIATION_URL = (
    "https://vendor-stage.ecpay.com.tw/PaymentMedia/TradeNoAio"
)


def post_form(url: str, fields: Mapping[str, str]) -> str:
    """POST a UTF-8 form to a stage endpoint and return its UTF-8 response."""
    request = Request(
        url,
        data=urlencode(dict(fields)).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "twpay-checkout-sandbox/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - URL is stage-asserted
        return response.read().decode("utf-8", errors="replace")


def query_and_recover_order(
    session: Session,
    gateway: EcpayGateway,
    order: Order,
    *,
    transport: FormTransport = post_form,
) -> GatewayQueryLog:
    """Query ECPay and recover a missed paid callback only after all checks pass."""
    request = gateway.build_query_request(order.order_no)
    try:
        body = transport(request.action_url, request.fields)
        result = gateway.parse_query_response(body)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        log = GatewayQueryLog(
            order_no=order.order_no,
            outcome=QueryOutcome.GATEWAY_ERROR,
            raw_payload=str(exc),
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        return log

    order_matched = result.order_no == order.order_no
    amount_matched = result.amount == order.amount
    if not result.signature_valid:
        outcome = QueryOutcome.REJECTED_SIGNATURE
    elif not order_matched:
        outcome = QueryOutcome.REJECTED_ORDER
    elif not amount_matched:
        outcome = QueryOutcome.REJECTED_AMOUNT
    elif result.trade_status == "1":
        if order.status == OrderStatus.PAID:
            outcome = QueryOutcome.CONFIRMED_PAID
        elif order.status in (
            OrderStatus.PENDING,
            OrderStatus.AWAITING_PAYMENT,
            OrderStatus.EXPIRED,
        ):
            order.status = OrderStatus.PAID
            order.gateway_trade_no = result.gateway_trade_no
            order.paid_at = result.payment_date or datetime.now()
            session.add(order)
            outcome = QueryOutcome.RECOVERED
        else:
            outcome = QueryOutcome.GATEWAY_ERROR
    elif result.trade_status == "0":
        outcome = QueryOutcome.UNPAID
    else:
        outcome = QueryOutcome.GATEWAY_ERROR

    log = GatewayQueryLog(
        order_no=order.order_no,
        gateway_trade_no=result.gateway_trade_no or None,
        remote_status=result.trade_status or None,
        remote_amount=result.amount,
        signature_valid=result.signature_valid,
        order_matched=order_matched,
        amount_matched=amount_matched,
        outcome=outcome,
        raw_payload=body,
    )
    session.add(log)
    session.commit()
    session.refresh(log)
    return log


def cancel_subscription(
    session: Session,
    gateway: EcpayGateway,
    subscription: Subscription,
    order: Order,
    *,
    transport: FormTransport = post_form,
) -> SubscriptionActionLog:
    """Cancel an active ECPay recurring order through the signed stage API."""
    if subscription.status not in {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.PAST_DUE,
    }:
        log = SubscriptionActionLog(
            subscription_id=subscription.id or 0,
            action=SubscriptionAction.CANCEL,
            status=SubscriptionActionStatus.REJECTED,
            gateway_message="只有 active／past_due 計畫可終止",
            raw_payload="local_policy_rejected",
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        return log

    request = gateway.build_period_action_request(
        order.order_no, action=SubscriptionAction.CANCEL.value
    )
    try:
        body = transport(request.action_url, request.fields)
        result = gateway.parse_period_action_response(body)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        log = SubscriptionActionLog(
            subscription_id=subscription.id or 0,
            action=SubscriptionAction.CANCEL,
            status=SubscriptionActionStatus.GATEWAY_ERROR,
            gateway_message=str(exc),
            raw_payload=str(exc),
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        return log

    applied = (
        result.signature_valid
        and result.order_no == order.order_no
        and result.code == "1"
    )
    log = SubscriptionActionLog(
        subscription_id=subscription.id or 0,
        action=SubscriptionAction.CANCEL,
        status=(
            SubscriptionActionStatus.APPLIED
            if applied
            else SubscriptionActionStatus.REJECTED
        ),
        signature_valid=result.signature_valid,
        gateway_code=result.code or None,
        gateway_message=result.message,
        raw_payload=body,
    )
    if applied:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.last_message = result.message
        subscription.updated_at = datetime.now()
        session.add(subscription)
    session.add(log)
    session.commit()
    session.refresh(log)
    return log


def query_and_sync_subscription(
    session: Session,
    gateway: EcpayGateway,
    subscription: Subscription,
    order: Order,
    *,
    transport: FormTransport = post_form,
) -> SubscriptionQueryLog:
    """Query recurring details and recover missing charge-ledger rows safely."""
    request = gateway.build_period_query_request(order.order_no)
    try:
        body = transport(request.action_url, request.fields)
        result = gateway.parse_period_query_response(body)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        log = SubscriptionQueryLog(
            subscription_id=subscription.id or 0,
            outcome=SubscriptionQueryOutcome.GATEWAY_ERROR,
            gateway_message=str(exc),
            raw_payload=str(exc),
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        return log

    order_matched = (
        result.merchant_id == gateway.merchant_id
        and result.order_no == order.order_no
    )
    successful_remote_logs = [
        entry for entry in result.exec_log if str(entry.get("RtnCode", "")) == "1"
    ]
    log_amounts_match = all(
        _safe_int(entry.get("amount")) == order.amount
        for entry in successful_remote_logs
    )
    amount_matched = result.period_amount == order.amount and log_amounts_match
    schedule_matched = (
        result.period_type == subscription.period_type
        and result.frequency == subscription.frequency
        and result.exec_times == subscription.exec_times
    )
    accepted = (
        result.code == "1"
        and order_matched
        and amount_matched
        and schedule_matched
        and result.total_success_times is not None
        and result.total_success_amount is not None
    )
    recovered = 0
    if accepted:
        existing_trade_nos = {
            charge.gateway_trade_no
            for charge in session.exec(
                select(SubscriptionCharge).where(
                    SubscriptionCharge.subscription_id == subscription.id
                )
            ).all()
        }
        for sequence, entry in enumerate(result.exec_log, start=1):
            trade_no = str(entry.get("TradeNo", ""))
            amount = _safe_int(entry.get("amount"))
            success = str(entry.get("RtnCode", "")) == "1"
            if not trade_no or trade_no in existing_trade_nos or amount is None:
                continue
            charged_at = _parse_ecpay_datetime(str(entry.get("process_date", "")))
            session.add(
                SubscriptionCharge(
                    subscription_id=subscription.id or 0,
                    gateway_trade_no=trade_no,
                    sequence=sequence,
                    amount=amount,
                    success=success,
                    amount_matched=amount == order.amount,
                    message="Recovered by QueryCreditCardPeriodInfo",
                    raw_payload=str(entry),
                    charged_at=charged_at or datetime.now(),
                )
            )
            existing_trade_nos.add(trade_no)
            recovered += 1

        subscription.success_times = result.total_success_times or 0
        subscription.success_amount = result.total_success_amount or 0
        subscription.failure_times = sum(
            str(entry.get("RtnCode", "")) != "1" for entry in result.exec_log
        )
        subscription.status = {
            "0": SubscriptionStatus.CANCELED,
            "1": SubscriptionStatus.ACTIVE,
            "2": SubscriptionStatus.COMPLETED,
        }.get(result.exec_status, subscription.status)
        subscription.last_message = "Recurring detail synchronized from ECPay stage"
        subscription.updated_at = datetime.now()
        session.add(subscription)

    log = SubscriptionQueryLog(
        subscription_id=subscription.id or 0,
        outcome=(
            SubscriptionQueryOutcome.SYNCED
            if accepted
            else SubscriptionQueryOutcome.REJECTED
        ),
        order_matched=order_matched,
        amount_matched=amount_matched,
        schedule_matched=schedule_matched,
        remote_exec_status=result.exec_status or None,
        remote_success_times=result.total_success_times,
        recovered_charges=recovered,
        gateway_message=result.message,
        raw_payload=body,
    )
    session.add(log)
    session.commit()
    session.refresh(log)
    return log


def _safe_int(value: object) -> int | None:
    text = str(value)
    return int(text) if text.isdigit() else None


def _parse_ecpay_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def simulate_refund(
    session: Session,
    order: Order,
    *,
    amount: int,
    action: RefundAction,
    reason: str,
    idempotency_key: str | None = None,
) -> RefundRequest:
    """Run the refund policy engine without calling a production-only API."""
    key = idempotency_key or secrets.token_urlsafe(18)
    existing = session.exec(
        select(RefundRequest).where(RefundRequest.idempotency_key == key)
    ).first()
    if existing is not None:
        return existing

    previous = (
        session.exec(
            select(RefundRequest).where(
                RefundRequest.order_id == order.id,
                RefundRequest.status == RefundStatus.SIMULATED_SUCCEEDED,
            )
        ).all()
        if order.id is not None
        else []
    )
    remaining = order.amount - sum(item.amount for item in previous)
    rejection = ""
    if order.gateway != GatewayName.ECPAY:
        rejection = "展示版退款流程只涵蓋 ECPay"
    elif order.payment_method != PaymentMethod.CREDIT_CARD:
        rejection = "ATM 訂單不適用信用卡請退款流程"
    elif order.status != OrderStatus.PAID:
        rejection = "只有已付款訂單可建立退款作業"
    elif amount <= 0:
        rejection = "退款金額必須大於零"
    elif amount > remaining:
        rejection = f"退款金額超過剩餘可退金額 NT$ {remaining}"
    elif action == RefundAction.VOID and amount != remaining:
        rejection = "放棄授權只能處理剩餘全額"

    row = RefundRequest(
        order_id=order.id or 0,
        idempotency_key=key,
        action=action,
        amount=amount,
        reason=reason.strip()[:200],
        status=(
            RefundStatus.REJECTED
            if rejection
            else RefundStatus.SIMULATED_SUCCEEDED
        ),
        gateway_message=(
            rejection
            or "Sandbox simulation only — ECPay 官方未提供信用卡請退款測試端點"
        ),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def build_reconciliation_request(
    gateway: EcpayGateway,
    *,
    period_start: date,
    period_end: date,
) -> PaymentRedirect:
    """Build the signed stage request used after configuring an IP allowlist."""
    host = urlsplit(STAGE_RECONCILIATION_URL).hostname or ""
    if host != "vendor-stage.ecpay.com.tw":
        raise AssertionError("Refusing non-stage ECPay reconciliation endpoint")
    params = {
        "MerchantID": gateway.merchant_id,
        "DateType": "2",
        "BeginDate": period_start.isoformat(),
        "EndDate": period_end.isoformat(),
        "MediaFormated": "2",
        "CharSet": "2",
    }
    params["CheckMacValue"] = generate_check_mac_value(
        params, gateway.hash_key, gateway.hash_iv
    )
    return PaymentRedirect(action_url=STAGE_RECONCILIATION_URL, fields=params)


def _value(row: Mapping[str, str], aliases: tuple[str, ...]) -> str:
    normalized = {
        str(key).replace("\ufeff", "").strip(): (value or "").strip()
        for key, value in row.items()
    }
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return ""


def _gateway_is_paid(value: str) -> bool:
    return value.strip().lower() in {
        "1",
        "paid",
        "success",
        "已付款",
        "付款成功",
        "交易成功",
    }


def reconcile_csv(
    session: Session,
    *,
    csv_text: str,
    source_name: str,
    period_start: date,
    period_end: date,
) -> ReconciliationRun:
    """Import an ECPay CSV and persist a non-mutating difference report."""
    run = ReconciliationRun(
        period_start=period_start,
        period_end=period_end,
        source_name=source_name[:255] or "uploaded.csv",
        status=ReconciliationStatus.COMPLETED,
    )
    session.add(run)
    session.flush()
    try:
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
        if not reader.fieldnames:
            raise ValueError("CSV 缺少標題列")
        gateway_rows: dict[str, dict[str, str | int | None]] = {}
        for source_row in reader:
            order_no = _value(
                source_row,
                ("MerchantTradeNo", "特店交易編號", "廠商訂單編號"),
            )
            if not order_no:
                continue
            amount_text = _value(
                source_row, ("TradeAmt", "交易金額", "訂單金額", "金額")
            ).replace(",", "")
            gateway_rows[order_no] = {
                "trade_no": _value(
                    source_row, ("TradeNo", "綠界交易編號", "交易編號")
                )
                or None,
                "amount": int(amount_text) if amount_text.isdigit() else None,
                "status": _value(
                    source_row, ("PaymentStatus", "付款狀態", "交易狀態")
                ),
            }

        local_orders = session.exec(
            select(Order).where(
                Order.gateway == GatewayName.ECPAY,
                Order.created_at >= datetime.combine(period_start, datetime.min.time()),
                Order.created_at
                <= datetime.combine(period_end, datetime.max.time()),
            )
        ).all()
        local_by_no = {order.order_no: order for order in local_orders}
        items: list[ReconciliationItem] = []

        for order_no, gateway_row in gateway_rows.items():
            order = local_by_no.get(order_no)
            if order is None:
                result = ReconciliationResult.MISSING_LOCAL
                local_amount = None
                local_status = None
            else:
                local_amount = order.amount
                local_status = order.status.value
                gateway_amount = gateway_row["amount"]
                gateway_status = str(gateway_row["status"]).strip()
                if gateway_amount != order.amount:
                    result = ReconciliationResult.AMOUNT_MISMATCH
                elif not gateway_status or _gateway_is_paid(gateway_status) != (
                    order.status == OrderStatus.PAID
                ):
                    result = ReconciliationResult.STATUS_MISMATCH
                else:
                    result = ReconciliationResult.MATCHED
            items.append(
                ReconciliationItem(
                    run_id=run.id or 0,
                    order_no=order_no,
                    gateway_trade_no=str(gateway_row["trade_no"] or "") or None,
                    local_amount=local_amount,
                    gateway_amount=(
                        gateway_row["amount"]
                        if isinstance(gateway_row["amount"], int)
                        else None
                    ),
                    local_status=local_status,
                    gateway_status=str(gateway_row["status"] or ""),
                    result=result,
                )
            )

        for order_no, order in local_by_no.items():
            if order_no not in gateway_rows:
                items.append(
                    ReconciliationItem(
                        run_id=run.id or 0,
                        order_no=order_no,
                        gateway_trade_no=order.gateway_trade_no,
                        local_amount=order.amount,
                        local_status=order.status.value,
                        result=ReconciliationResult.MISSING_GATEWAY,
                    )
                )

        run.gateway_rows = len(gateway_rows)
        run.local_rows = len(local_orders)
        run.matched_rows = sum(
            item.result == ReconciliationResult.MATCHED for item in items
        )
        run.difference_rows = len(items) - run.matched_rows
        for item in items:
            session.add(item)
        session.add(run)
        session.commit()
        session.refresh(run)
        return run
    except (UnicodeError, ValueError, csv.Error) as exc:
        session.rollback()
        failed = ReconciliationRun(
            period_start=period_start,
            period_end=period_end,
            source_name=source_name[:255] or "uploaded.csv",
            status=ReconciliationStatus.FAILED,
            error_message=str(exc),
        )
        session.add(failed)
        session.commit()
        session.refresh(failed)
        return failed
