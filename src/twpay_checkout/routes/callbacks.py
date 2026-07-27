"""Gateway callback endpoints.

背景通知（notify / payment-info）是訂單狀態的唯一寫入路徑；
前景導回（return）只做顯示——導向訂單頁，頁面內容一律讀 DB。
兩家的前景導回都是 Form POST，所以這裡全部開 POST。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from ..config import Settings
from ..deps import get_session, get_settings, templates
from ..gateways import get_gateway
from ..gateways.base import SignatureError
from ..models import GatewayName, NotificationKind, Order
from ..services.payments import process_notification, process_recurring_notification

router = APIRouter(prefix="/callbacks")


def _repair_raw_utf8(value: str) -> str:
    """Repair UTF-8 bytes decoded as Latin-1 by form parsers.

    ECPay's live callback can place raw UTF-8 bytes in an
    ``application/x-www-form-urlencoded`` body instead of percent-encoding
    them. Starlette's URL-encoded form parser decodes raw field bytes as
    Latin-1, which turns ``交易成功`` into mojibake and breaks CheckMacValue.

    Correctly decoded Unicode cannot be encoded as Latin-1 and is returned
    unchanged; ASCII values round-trip without modification.
    """
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


async def _form_dict(request: Request) -> dict[str, str]:
    form = await request.form()
    return {
        key: _repair_raw_utf8(value)
        for key, value in form.items()
        if isinstance(value, str)
    }


def _redirect_to_order(request: Request, session: Session, order_no: str):
    order = session.exec(select(Order).where(Order.order_no == order_no)).first()
    if order is not None:
        return RedirectResponse(f"/orders/{order_no}", status_code=303)
    return templates.TemplateResponse(
        request,
        "message.html",
        {"title": "找不到訂單", "text": f"導回的訂單編號不存在：{order_no or '(空白)'}"},
        status_code=404,
    )


# ---- ECPay ----


@router.post("/ecpay/notify")
async def ecpay_notify(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """ECPay ReturnURL：付款結果背景通知（成功須回純文字 1|OK）。"""
    payload = await _form_dict(request)
    gateway = get_gateway(GatewayName.ECPAY, settings)
    return process_notification(session, gateway, payload, NotificationKind.PAYMENT_RESULT)


@router.post("/ecpay/payment-info")
async def ecpay_payment_info(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """ECPay PaymentInfoURL：ATM 取號結果背景通知（RtnCode=2 為成功）。"""
    payload = await _form_dict(request)
    gateway = get_gateway(GatewayName.ECPAY, settings)
    return process_notification(
        session, gateway, payload, NotificationKind.ATM_ACCOUNT_ISSUED
    )


@router.post("/ecpay/period")
async def ecpay_period(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """ECPay PeriodReturnURL：第二期起的定期定額授權通知。"""
    payload = await _form_dict(request)
    gateway = get_gateway(GatewayName.ECPAY, settings)
    return process_recurring_notification(session, gateway, payload)


@router.post("/ecpay/return")
async def ecpay_front_return(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
):
    """ECPay OrderResultURL（信用卡）／ClientRedirectURL（ATM 取號）前景導回。

    只做顯示：導向訂單頁，狀態一律以 DB（背景通知的結果）為準。
    """
    payload = await _form_dict(request)
    return _redirect_to_order(request, session, payload.get("MerchantTradeNo", ""))


# ---- NewebPay ----


@router.post("/newebpay/notify")
async def newebpay_notify(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """藍新 NotifyURL：付款結果背景通知（官方只認 HTTP 200 為成功回應）。"""
    payload = await _form_dict(request)
    gateway = get_gateway(GatewayName.NEWEBPAY, settings)
    return process_notification(session, gateway, payload, NotificationKind.PAYMENT_RESULT)


@router.post("/newebpay/customer")
async def newebpay_customer(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """藍新 CustomerURL：ATM 取號完成導回（瀏覽器 Form POST）。

    藍新的取號「沒有」背景通知，這是唯一的取號資訊來源——payload 同樣是
    TradeSha 驗簽＋AES 加密格式，驗簽通過後允許 pending→awaiting_payment
    這一個轉移（永不觸發入帳），處理完把瀏覽器導到訂單頁。
    """
    payload = await _form_dict(request)
    gateway = get_gateway(GatewayName.NEWEBPAY, settings)
    process_notification(session, gateway, payload, NotificationKind.ATM_ACCOUNT_ISSUED)
    try:
        parsed = gateway.verify_and_parse_notification(
            payload, NotificationKind.ATM_ACCOUNT_ISSUED
        )
    except SignatureError:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"title": "取號導回驗證失敗", "text": "取號結果驗簽失敗，已記錄原始通知供稽核。"},
            status_code=400,
        )
    return _redirect_to_order(request, session, parsed.order_no)


@router.post("/newebpay/return")
async def newebpay_front_return(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """藍新 ReturnURL：付款完成前景導回（Form POST），只做顯示。

    payload 是加密的 TradeInfo，需解密才知道訂單編號；不做任何狀態變更，
    訂單頁內容一律以背景通知寫入的 DB 狀態為準。
    """
    payload = await _form_dict(request)
    gateway = get_gateway(GatewayName.NEWEBPAY, settings)
    try:
        parsed = gateway.verify_and_parse_notification(
            payload, NotificationKind.PAYMENT_RESULT
        )
    except SignatureError:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"title": "導回驗證失敗", "text": "前景導回的資料驗簽失敗；訂單實際狀態以背景通知為準。"},
            status_code=400,
        )
    return _redirect_to_order(request, session, parsed.order_no)
