"""Payment operations dashboard and mutation endpoints."""
import secrets
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from ..config import Settings
from ..deps import get_session, get_settings, templates
from ..gateways.ecpay import EcpayGateway
from ..models import (
    GatewayName,
    GatewayQueryLog,
    Order,
    ReconciliationItem,
    ReconciliationRun,
    RefundAction,
    RefundRequest,
    Subscription,
    SubscriptionActionLog,
    SubscriptionCharge,
    SubscriptionQueryLog,
)
from ..services.operations import (
    FormTransport,
    cancel_subscription,
    query_and_recover_order,
    query_and_sync_subscription,
    reconcile_csv,
    simulate_refund,
)

router = APIRouter(prefix="/admin")


def _order(session: Session, order_no: str) -> Order:
    order = session.exec(select(Order).where(Order.order_no == order_no)).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Unknown order")
    return order


@router.get("/operations", response_class=HTMLResponse)
def operations_dashboard(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
):
    orders = session.exec(
        select(Order)
        .where(Order.gateway == GatewayName.ECPAY)
        .order_by(Order.id.desc())
        .limit(30)
    ).all()
    subscriptions = session.exec(
        select(Subscription).order_by(Subscription.id.desc()).limit(30)
    ).all()
    charges = session.exec(
        select(SubscriptionCharge).order_by(SubscriptionCharge.id.desc()).limit(50)
    ).all()
    action_logs = session.exec(
        select(SubscriptionActionLog)
        .order_by(SubscriptionActionLog.id.desc())
        .limit(50)
    ).all()
    subscription_query_logs = session.exec(
        select(SubscriptionQueryLog)
        .order_by(SubscriptionQueryLog.id.desc())
        .limit(50)
    ).all()
    query_logs = session.exec(
        select(GatewayQueryLog).order_by(GatewayQueryLog.id.desc()).limit(50)
    ).all()
    refunds = session.exec(
        select(RefundRequest).order_by(RefundRequest.id.desc()).limit(50)
    ).all()
    runs = session.exec(
        select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(20)
    ).all()
    run_items: dict[int, list[ReconciliationItem]] = {}
    for run in runs:
        if run.id is not None:
            run_items[run.id] = session.exec(
                select(ReconciliationItem)
                .where(ReconciliationItem.run_id == run.id)
                .order_by(ReconciliationItem.id)
            ).all()
    order_by_id = {order.id: order for order in orders if order.id is not None}
    subscription_by_order = {
        item.order_id: item for item in subscriptions
    }
    refundable = sum(
        1
        for order in orders
        if order.status.value == "paid" and order.payment_method.value == "credit_card"
    )
    return templates.TemplateResponse(
        request,
        "operations.html",
        {
            "orders": orders,
            "subscriptions": subscriptions,
            "charges": charges,
            "action_logs": action_logs,
            "subscription_query_logs": subscription_query_logs,
            "query_logs": query_logs,
            "refunds": refunds,
            "runs": runs,
            "run_items": run_items,
            "order_by_id": order_by_id,
            "subscription_by_order": subscription_by_order,
            "refund_tokens": {
                order.order_no: secrets.token_urlsafe(18) for order in orders
            },
            "today": date.today().isoformat(),
            "metrics": {
                "orders": len(orders),
                "subscriptions": len(subscriptions),
                "refundable": refundable,
                "differences": sum(run.difference_rows for run in runs),
            },
        },
    )


@router.post("/subscriptions/{subscription_id}/query")
def query_recurring(
    request: Request,
    subscription_id: int,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    subscription = session.get(Subscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Unknown subscription")
    order = session.get(Order, subscription.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Subscription order is missing")
    transport: FormTransport | None = getattr(
        request.app.state, "ecpay_form_transport", None
    )
    log = query_and_sync_subscription(
        session,
        EcpayGateway(settings),
        subscription,
        order,
        **({"transport": transport} if transport is not None else {}),
    )
    return RedirectResponse(
        f"/admin/operations?period_query={log.outcome.value}#subscriptions",
        status_code=303,
    )


@router.post("/subscriptions/{subscription_id}/cancel")
def cancel_recurring(
    request: Request,
    subscription_id: int,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    subscription = session.get(Subscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Unknown subscription")
    order = session.get(Order, subscription.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Subscription order is missing")
    transport: FormTransport | None = getattr(
        request.app.state, "ecpay_form_transport", None
    )
    log = cancel_subscription(
        session,
        EcpayGateway(settings),
        subscription,
        order,
        **({"transport": transport} if transport is not None else {}),
    )
    return RedirectResponse(
        f"/admin/operations?subscription={log.status.value}#subscriptions",
        status_code=303,
    )


@router.post("/orders/{order_no}/query")
def query_order(
    request: Request,
    order_no: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    order = _order(session, order_no)
    if order.gateway != GatewayName.ECPAY:
        raise HTTPException(status_code=422, detail="Only ECPay query is available")
    transport: FormTransport | None = getattr(
        request.app.state, "ecpay_form_transport", None
    )
    gateway = EcpayGateway(settings)
    log = query_and_recover_order(
        session,
        gateway,
        order,
        **({"transport": transport} if transport is not None else {}),
    )
    return RedirectResponse(
        f"/admin/operations?query={log.outcome.value}#queries", status_code=303
    )


@router.post("/orders/{order_no}/refund")
def refund_order(
    order_no: str,
    session: Annotated[Session, Depends(get_session)],
    amount: Annotated[int, Form()],
    action: Annotated[RefundAction, Form()],
    idempotency_key: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
) -> RedirectResponse:
    order = _order(session, order_no)
    refund = simulate_refund(
        session,
        order,
        amount=amount,
        action=action,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    return RedirectResponse(
        f"/admin/operations?refund={refund.status.value}#refunds", status_code=303
    )


@router.post("/reconciliation")
async def upload_reconciliation(
    session: Annotated[Session, Depends(get_session)],
    report: UploadFile,
    period_start: Annotated[date, Form()],
    period_end: Annotated[date, Form()],
) -> RedirectResponse:
    if period_start > period_end:
        raise HTTPException(status_code=422, detail="period_start must be <= period_end")
    content = await report.read(2_000_001)
    if len(content) > 2_000_000:
        raise HTTPException(status_code=413, detail="CSV exceeds 2 MB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="CSV must be UTF-8") from exc
    run = reconcile_csv(
        session,
        csv_text=text,
        source_name=report.filename or "uploaded.csv",
        period_start=period_start,
        period_end=period_end,
    )
    return RedirectResponse(
        f"/admin/operations?reconciliation={run.status.value}#reconciliation",
        status_code=303,
    )
