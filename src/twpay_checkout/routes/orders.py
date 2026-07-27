"""Order detail page, status API (polled by the browser), and audit page."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from ..deps import get_session, templates
from ..models import (
    GatewayQueryLog,
    Order,
    PaymentNotification,
    Product,
    RefundRequest,
    Subscription,
    SubscriptionCharge,
)
from ..services.payments import effective_status

router = APIRouter()


def _get_order(session: Session, order_no: str) -> Order:
    order = session.exec(select(Order).where(Order.order_no == order_no)).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Unknown order")
    return order


@router.get("/orders/{order_no}", response_class=HTMLResponse)
def order_detail(
    request: Request,
    order_no: str,
    session: Annotated[Session, Depends(get_session)],
):
    order = _get_order(session, order_no)
    # expired 純衍生、不落庫：DB 保持 awaiting_payment，逾期後才到的真實
    # 入帳通知（銀行已實收）仍能正常入帳。
    status = effective_status(order)
    product = session.get(Product, order.product_id)
    notifications = session.exec(
        select(PaymentNotification)
        .where(PaymentNotification.order_no == order_no)
        .order_by(PaymentNotification.id)
    ).all()
    subscription = (
        session.exec(
            select(Subscription).where(Subscription.order_id == order.id)
        ).first()
        if order.id is not None
        else None
    )
    charges = (
        session.exec(
            select(SubscriptionCharge)
            .where(SubscriptionCharge.subscription_id == subscription.id)
            .order_by(SubscriptionCharge.sequence)
        ).all()
        if subscription is not None and subscription.id is not None
        else []
    )
    query_logs = session.exec(
        select(GatewayQueryLog)
        .where(GatewayQueryLog.order_no == order_no)
        .order_by(GatewayQueryLog.id.desc())
    ).all()
    refunds = (
        session.exec(
            select(RefundRequest)
            .where(RefundRequest.order_id == order.id)
            .order_by(RefundRequest.id.desc())
        ).all()
        if order.id is not None
        else []
    )
    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {
            "order": order,
            "status": status,
            "product": product,
            "notifications": notifications,
            "subscription": subscription,
            "charges": charges,
            "query_logs": query_logs,
            "refunds": refunds,
        },
    )


@router.get("/api/orders/{order_no}/status")
def order_status(order_no: str, session: Annotated[Session, Depends(get_session)]):
    order = _get_order(session, order_no)
    status = effective_status(order)
    return {
        "order_no": order.order_no,
        "status": status.value,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "atm": {
            "bank_code": order.bank_code,
            "virtual_account": order.virtual_account,
            "pay_deadline": order.pay_deadline.isoformat() if order.pay_deadline else None,
        }
        if order.virtual_account
        else None,
    }


@router.get("/admin/notifications", response_class=HTMLResponse)
def notification_audit(request: Request, session: Annotated[Session, Depends(get_session)]):
    notifications = session.exec(
        select(PaymentNotification).order_by(PaymentNotification.id.desc()).limit(100)
    ).all()
    return templates.TemplateResponse(
        request, "notifications.html", {"notifications": notifications}
    )
