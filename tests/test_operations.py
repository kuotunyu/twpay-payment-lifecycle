"""Payment lifecycle tests: recurring, recovery, refund policy, reconciliation."""
import json
from datetime import date

from sqlmodel import Session, select

from twpay_checkout.gateways.ecpay import generate_check_mac_value
from twpay_checkout.models import (
    GatewayQueryLog,
    Order,
    PaymentNotification,
    QueryOutcome,
    ReconciliationItem,
    ReconciliationResult,
    ReconciliationRun,
    RefundRequest,
    RefundStatus,
    Subscription,
    SubscriptionActionLog,
    SubscriptionActionStatus,
    SubscriptionCharge,
    SubscriptionQueryLog,
    SubscriptionQueryOutcome,
    SubscriptionStatus,
)

from conftest import (
    ECPAY_HASH_IV,
    ECPAY_HASH_KEY,
    ECPAY_MERCHANT_ID,
    order_status,
    place_order,
)
from test_notifications import ecpay_payment_payload


def place_subscription(client, product_id: int = 1) -> str:
    response = client.post(
        "/orders",
        data={
            "product_id": str(product_id),
            "gateway": "ecpay",
            "method": "credit_card",
            "plan": "monthly_3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def ecpay_period_payload(order_no: str, **overrides) -> dict[str, str]:
    payload = {
        "MerchantID": ECPAY_MERCHANT_ID,
        "MerchantTradeNo": order_no,
        "TradeNo": f"PERIOD{order_no[1:13]}",
        "RtnCode": "1",
        "RtnMsg": "定期定額授權成功",
        "PeriodType": "M",
        "Frequency": "1",
        "ExecTimes": "3",
        "Amount": "450",
        "ProcessDate": "2026/07/26 12:00:00",
        "TotalSuccessTimes": "2",
        "TotalSuccessAmount": "900",
        **overrides,
    }
    payload["CheckMacValue"] = generate_check_mac_value(
        payload, ECPAY_HASH_KEY, ECPAY_HASH_IV
    )
    return payload


def query_response(order_no: str, **overrides) -> str:
    payload = {
        "MerchantID": ECPAY_MERCHANT_ID,
        "MerchantTradeNo": order_no,
        "TradeNo": f"QUERY{order_no[1:13]}",
        "TradeAmt": "450",
        "TradeStatus": "1",
        "PaymentDate": "2026/07/26 12:30:00",
        **overrides,
    }
    payload["CheckMacValue"] = generate_check_mac_value(
        payload, ECPAY_HASH_KEY, ECPAY_HASH_IV
    )
    from urllib.parse import urlencode

    return urlencode(payload)


def period_action_response(order_no: str, **overrides) -> str:
    payload = {
        "RtnCode": "1",
        "RtnMsg": "取消成功",
        "MerchantID": ECPAY_MERCHANT_ID,
        "MerchantTradeNo": order_no,
        **overrides,
    }
    payload["CheckMacValue"] = generate_check_mac_value(
        payload, ECPAY_HASH_KEY, ECPAY_HASH_IV
    )
    from urllib.parse import urlencode

    return urlencode(payload)


def period_query_response(order_no: str, **overrides) -> str:
    payload = {
        "MerchantID": ECPAY_MERCHANT_ID,
        "MerchantTradeNo": order_no,
        "TradeNo": "PERIOD-FIRST-001",
        "RtnCode": 1,
        "RtnMsg": "Success",
        "PeriodType": "M",
        "Frequency": 1,
        "ExecTimes": 3,
        "PeriodAmount": 450,
        "TotalSuccessTimes": 2,
        "TotalSuccessAmount": 900,
        "ExecStatus": "1",
        "ExecLog": [
            {
                "RtnCode": 1,
                "amount": 450,
                "process_date": "2026/07/26 12:00:00",
                "TradeNo": "PERIOD-FIRST-001",
            },
            {
                "RtnCode": 1,
                "amount": 450,
                "process_date": "2026/08/26 12:00:00",
                "TradeNo": "PERIOD-SECOND-002",
            },
        ],
        **overrides,
    }
    return json.dumps(payload, ensure_ascii=False)


def test_recurring_checkout_has_signed_period_fields(client, app):
    order_no = place_subscription(client)
    html = client.get(f"/orders/{order_no}/checkout").text

    assert 'name="PeriodType" value="M"' in html
    assert 'name="Frequency" value="1"' in html
    assert 'name="ExecTimes" value="3"' in html
    assert 'name="PeriodAmount" value="450"' in html
    assert "/callbacks/ecpay/period" in html

    with Session(app.state.engine) as session:
        order = session.exec(select(Order).where(Order.order_no == order_no)).one()
        subscription = session.exec(
            select(Subscription).where(Subscription.order_id == order.id)
        ).one()
        assert subscription.status == SubscriptionStatus.PENDING


def test_period_notification_updates_subscription_idempotently(client, app):
    order_no = place_subscription(client)
    first_auth = client.post(
        "/callbacks/ecpay/notify", data=ecpay_payment_payload(order_no)
    )
    period_payload = ecpay_period_payload(order_no)
    first_period = client.post("/callbacks/ecpay/period", data=period_payload)
    duplicate = client.post("/callbacks/ecpay/period", data=period_payload)

    assert first_auth.text == "1|OK"
    assert first_period.text == "1|OK"
    assert duplicate.text == "1|OK"
    with Session(app.state.engine) as session:
        order = session.exec(select(Order).where(Order.order_no == order_no)).one()
        subscription = session.exec(
            select(Subscription).where(Subscription.order_id == order.id)
        ).one()
        charges = session.exec(
            select(SubscriptionCharge).where(
                SubscriptionCharge.subscription_id == subscription.id
            )
        ).all()
        recurring_audit = session.exec(
            select(PaymentNotification).where(
                PaymentNotification.order_no == order_no,
                PaymentNotification.kind == "recurring_payment",
            )
        ).all()
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.success_times == 2
        assert subscription.success_amount == 900
        assert len(charges) == 1
        assert len(recurring_audit) == 2


def test_active_subscription_can_be_canceled_via_signed_stage_action(client, app):
    order_no = place_subscription(client)
    client.post("/callbacks/ecpay/notify", data=ecpay_payment_payload(order_no))
    app.state.ecpay_form_transport = (
        lambda _url, _fields: period_action_response(order_no)
    )
    with Session(app.state.engine) as session:
        order = session.exec(select(Order).where(Order.order_no == order_no)).one()
        subscription = session.exec(
            select(Subscription).where(Subscription.order_id == order.id)
        ).one()
        subscription_id = subscription.id

    response = client.post(
        f"/admin/subscriptions/{subscription_id}/cancel",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "subscription=applied" in response.headers["location"]
    with Session(app.state.engine) as session:
        subscription = session.get(Subscription, subscription_id)
        log = session.exec(select(SubscriptionActionLog)).one()
        assert subscription.status == SubscriptionStatus.CANCELED
        assert log.status == SubscriptionActionStatus.APPLIED
        assert log.signature_valid


def test_period_query_recovers_missing_charge_rows_idempotently(client, app):
    order_no = place_subscription(client)
    client.post("/callbacks/ecpay/notify", data=ecpay_payment_payload(order_no))
    app.state.ecpay_form_transport = (
        lambda _url, _fields: period_query_response(order_no)
    )
    with Session(app.state.engine) as session:
        order = session.exec(select(Order).where(Order.order_no == order_no)).one()
        subscription = session.exec(
            select(Subscription).where(Subscription.order_id == order.id)
        ).one()
        subscription_id = subscription.id

    first = client.post(
        f"/admin/subscriptions/{subscription_id}/query",
        follow_redirects=False,
    )
    second = client.post(
        f"/admin/subscriptions/{subscription_id}/query",
        follow_redirects=False,
    )

    assert "period_query=synced" in first.headers["location"]
    assert second.headers["location"] == first.headers["location"]
    with Session(app.state.engine) as session:
        subscription = session.get(Subscription, subscription_id)
        charges = session.exec(
            select(SubscriptionCharge).where(
                SubscriptionCharge.subscription_id == subscription_id
            )
        ).all()
        logs = session.exec(select(SubscriptionQueryLog)).all()
        assert subscription.success_times == 2
        assert subscription.success_amount == 900
        assert len(charges) == 2
        assert [log.recovered_charges for log in logs] == [2, 0]
        assert all(log.outcome == SubscriptionQueryOutcome.SYNCED for log in logs)


def test_query_recovers_missed_paid_callback(client, app):
    order_no = place_order(client, "ecpay", "credit_card")
    app.state.ecpay_form_transport = lambda _url, _fields: query_response(order_no)

    response = client.post(
        f"/admin/orders/{order_no}/query", follow_redirects=False
    )

    assert response.status_code == 303
    assert "query=recovered" in response.headers["location"]
    assert order_status(client, order_no) == "paid"
    with Session(app.state.engine) as session:
        log = session.exec(
            select(GatewayQueryLog).where(GatewayQueryLog.order_no == order_no)
        ).one()
        assert log.outcome == QueryOutcome.RECOVERED
        assert log.signature_valid and log.order_matched and log.amount_matched


def test_query_rejects_signed_amount_mismatch(client, app):
    order_no = place_order(client, "ecpay", "credit_card")
    app.state.ecpay_form_transport = lambda _url, _fields: query_response(
        order_no, TradeAmt="1"
    )

    response = client.post(
        f"/admin/orders/{order_no}/query", follow_redirects=False
    )

    assert "query=rejected_amount" in response.headers["location"]
    assert order_status(client, order_no) == "pending"


def test_refund_policy_is_simulated_idempotent_and_bounded(client, app):
    order_no = place_order(client, "ecpay", "credit_card")
    client.post("/callbacks/ecpay/notify", data=ecpay_payment_payload(order_no))
    form = {
        "amount": "300",
        "action": "refund",
        "reason": "客戶取消",
        "idempotency_key": "refund-operation-001",
    }

    first = client.post(
        f"/admin/orders/{order_no}/refund", data=form, follow_redirects=False
    )
    duplicate = client.post(
        f"/admin/orders/{order_no}/refund", data=form, follow_redirects=False
    )
    rejected = client.post(
        f"/admin/orders/{order_no}/refund",
        data={**form, "amount": "200", "idempotency_key": "refund-operation-002"},
        follow_redirects=False,
    )

    assert "refund=simulated_succeeded" in first.headers["location"]
    assert duplicate.headers["location"] == first.headers["location"]
    assert "refund=rejected" in rejected.headers["location"]
    with Session(app.state.engine) as session:
        rows = session.exec(select(RefundRequest)).all()
        assert len(rows) == 2
        assert rows[0].mode == "sandbox_simulation"
        assert rows[1].status == RefundStatus.REJECTED
        assert order_status(client, order_no) == "paid"


def test_reconciliation_reports_differences_without_mutating_orders(client, app):
    paid_no = place_order(client, "ecpay", "credit_card")
    blank_status_no = place_order(client, "ecpay", "credit_card")
    missing_gateway_no = place_order(client, "ecpay", "credit_card")
    client.post("/callbacks/ecpay/notify", data=ecpay_payment_payload(paid_no))
    csv_text = (
        "MerchantTradeNo,TradeNo,TradeAmt,PaymentStatus\n"
        f"{paid_no},GW-PAID,450,1\n"
        f"{blank_status_no},GW-BLANK,450,\n"
        "TEXTERNAL000000001,GW-ONLY,990,1\n"
    )
    today = date.today().isoformat()

    response = client.post(
        "/admin/reconciliation",
        data={"period_start": today, "period_end": today},
        files={"report": ("ecpay-v3.csv", csv_text.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "reconciliation=completed" in response.headers["location"]
    assert order_status(client, paid_no) == "paid"
    assert order_status(client, blank_status_no) == "pending"
    assert order_status(client, missing_gateway_no) == "pending"
    with Session(app.state.engine) as session:
        run = session.exec(select(ReconciliationRun)).one()
        items = session.exec(
            select(ReconciliationItem).where(ReconciliationItem.run_id == run.id)
        ).all()
        results = {item.result for item in items}
        assert run.matched_rows == 1
        assert run.difference_rows == 3
        assert results == {
            ReconciliationResult.MATCHED,
            ReconciliationResult.STATUS_MISMATCH,
            ReconciliationResult.MISSING_LOCAL,
            ReconciliationResult.MISSING_GATEWAY,
        }


def test_operations_dashboard_renders(client):
    response = client.get("/admin/operations")
    assert response.status_code == 200
    assert "付款營運台" in response.text
    assert "SIMULATED" in response.text
