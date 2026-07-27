"""Product listing and order creation."""
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from ..config import Settings
from ..deps import get_session, get_settings, templates
from ..gateways import get_gateway
from ..gateways.base import RecurringPlan
from ..models import GatewayName, Order, OrderStatus, PaymentMethod, Product, Subscription
from ..services.payments import (
    build_callback_urls,
    create_order,
    create_subscription,
    effective_status,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def product_list(request: Request, session: Annotated[Session, Depends(get_session)]):
    products = session.exec(select(Product)).all()
    recent_orders = session.exec(
        select(Order).order_by(Order.id.desc()).limit(8)
    ).all()
    statuses = {o.order_no: effective_status(o) for o in recent_orders}
    return templates.TemplateResponse(
        request,
        "products.html",
        {"products": products, "recent_orders": recent_orders, "statuses": statuses},
    )


@router.post("/orders")
def place_order(
    session: Annotated[Session, Depends(get_session)],
    product_id: Annotated[int, Form()],
    gateway: Annotated[GatewayName, Form()],
    method: Annotated[PaymentMethod, Form()],
    plan: Annotated[str, Form()] = "one_time",
) -> RedirectResponse:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Unknown product")
    if plan == "monthly_3" and (
        gateway != GatewayName.ECPAY or method != PaymentMethod.CREDIT_CARD
    ):
        raise HTTPException(
            status_code=422, detail="Recurring checkout requires ECPay credit card"
        )
    if plan not in {"one_time", "monthly_3"}:
        raise HTTPException(status_code=422, detail="Unknown billing plan")
    order = create_order(session, product, gateway, method)
    if plan == "monthly_3":
        create_subscription(session, order, period_type="M", frequency=1, exec_times=3)
    return RedirectResponse(f"/orders/{order.order_no}/checkout", status_code=303)


@router.get("/orders/{order_no}/checkout", response_class=HTMLResponse)
def checkout(
    request: Request,
    order_no: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """Render the auto-submit form that POSTs the signed fields to the gateway."""
    order = session.exec(select(Order).where(Order.order_no == order_no)).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Unknown order")
    if order.status != OrderStatus.PENDING:
        return RedirectResponse(f"/orders/{order.order_no}", status_code=303)
    product = session.get(Product, order.product_id)
    subscription = (
        session.exec(
            select(Subscription).where(Subscription.order_id == order.id)
        ).first()
        if order.id is not None
        else None
    )
    recurring = (
        RecurringPlan(
            period_type=subscription.period_type,
            frequency=subscription.frequency,
            exec_times=subscription.exec_times,
            period_amount=order.amount,
        )
        if subscription is not None
        else None
    )

    try:
        urls = build_callback_urls(settings.base_url, order.gateway)
        gateway = get_gateway(order.gateway, settings)
        redirect = gateway.create_payment(order, product.name, urls, recurring)
    except (ValueError, NotImplementedError) as exc:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"title": "尚無法付款", "text": str(exc),
             "back_url": f"/orders/{order.order_no}", "back_label": "查看訂單"},
            status_code=503,
        )

    label = "綠界 ECPay" if order.gateway == GatewayName.ECPAY else "藍新 NewebPay"
    return templates.TemplateResponse(
        request,
        "checkout_redirect.html",
        {"order": order, "redirect": redirect, "gateway_label": label},
    )
