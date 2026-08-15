"""High-ROI backend hardening tests: state invariants, failure injection, replay safety, and DB constraints."""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from twpay_checkout.models import (
    GatewayName,
    NotificationKind,
    Order,
    OrderStatus,
    PaymentMethod,
    PaymentNotification,
    ProcessedResult,
    Product,
    QueryOutcome,
    ReconciliationItem,
    ReconciliationResult,
    RefundAction,
    RefundRequest,
    RefundStatus,
    Subscription,
    SubscriptionCharge,
    SubscriptionStatus,
)
from twpay_checkout.services.operations import (
    query_and_recover_order,
    simulate_refund,
)
from twpay_checkout.services.transitions import (
    VALID_ORDER_TRANSITIONS,
    VALID_SUBSCRIPTION_TRANSITIONS,
    can_transition_order,
    can_transition_subscription,
)

from conftest import (
    ECPAY_HASH_IV,
    ECPAY_HASH_KEY,
    ECPAY_MERCHANT_ID,
    order_status,
    place_order,
)
from test_notifications import ecpay_payment_payload
from test_operations import (
    ecpay_period_payload,
    place_subscription,
    query_response,
)


# =============================================================================
# 1. Table-Driven State Transition Invariant Tests
# =============================================================================


@pytest.mark.parametrize(
    ("from_status", "to_status", "expected_allowed"),
    [
        # Order transitions
        (OrderStatus.PENDING, OrderStatus.AWAITING_PAYMENT, True),
        (OrderStatus.PENDING, OrderStatus.PAID, True),
        (OrderStatus.PENDING, OrderStatus.FAILED, True),
        (OrderStatus.PENDING, OrderStatus.PENDING, False),
        (OrderStatus.AWAITING_PAYMENT, OrderStatus.PAID, True),
        (OrderStatus.AWAITING_PAYMENT, OrderStatus.FAILED, True),
        (OrderStatus.AWAITING_PAYMENT, OrderStatus.PENDING, False),
        (OrderStatus.EXPIRED, OrderStatus.PAID, True),
        (OrderStatus.EXPIRED, OrderStatus.FAILED, False),
        # Terminal PAID invariants (must reject all reversals)
        (OrderStatus.PAID, OrderStatus.PENDING, False),
        (OrderStatus.PAID, OrderStatus.AWAITING_PAYMENT, False),
        (OrderStatus.PAID, OrderStatus.FAILED, False),
        (OrderStatus.PAID, OrderStatus.EXPIRED, False),
        # Terminal FAILED invariants
        (OrderStatus.FAILED, OrderStatus.PENDING, False),
        (OrderStatus.FAILED, OrderStatus.PAID, False),
    ],
)
def test_order_transition_matrix(from_status, to_status, expected_allowed):
    assert can_transition_order(from_status, to_status) is expected_allowed


@pytest.mark.parametrize(
    ("from_status", "to_status", "expected_allowed"),
    [
        # Subscription transitions
        (SubscriptionStatus.PENDING, SubscriptionStatus.ACTIVE, True),
        (SubscriptionStatus.PENDING, SubscriptionStatus.CANCELED, True),
        (SubscriptionStatus.PENDING, SubscriptionStatus.COMPLETED, False),
        (SubscriptionStatus.ACTIVE, SubscriptionStatus.ACTIVE, True),
        (SubscriptionStatus.ACTIVE, SubscriptionStatus.COMPLETED, True),
        (SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE, True),
        (SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELED, True),
        (SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING, False),
        (SubscriptionStatus.PAST_DUE, SubscriptionStatus.ACTIVE, True),
        (SubscriptionStatus.PAST_DUE, SubscriptionStatus.COMPLETED, True),
        (SubscriptionStatus.PAST_DUE, SubscriptionStatus.PAST_DUE, True),
        (SubscriptionStatus.PAST_DUE, SubscriptionStatus.CANCELED, True),
        # Terminal CANCELED invariants (late webhooks must not resurrect)
        (SubscriptionStatus.CANCELED, SubscriptionStatus.ACTIVE, False),
        (SubscriptionStatus.CANCELED, SubscriptionStatus.PAST_DUE, False),
        (SubscriptionStatus.CANCELED, SubscriptionStatus.COMPLETED, False),
        # Terminal COMPLETED invariants (late webhook must not demote)
        (SubscriptionStatus.COMPLETED, SubscriptionStatus.PAST_DUE, False),
        (SubscriptionStatus.COMPLETED, SubscriptionStatus.ACTIVE, False),
        (SubscriptionStatus.COMPLETED, SubscriptionStatus.CANCELED, False),
    ],
)
def test_subscription_transition_matrix(from_status, to_status, expected_allowed):
    assert can_transition_subscription(from_status, to_status) is expected_allowed


def test_late_recurring_notification_does_not_resurrect_canceled_subscription(
    client, app
):
    """A canceled recurring subscription must ignore late success or failure webhooks."""
    order_no = place_subscription(client)
    client.post("/callbacks/ecpay/notify", data=ecpay_payment_payload(order_no))

    with Session(app.state.engine) as session:
        order = session.exec(select(Order).where(Order.order_no == order_no)).one()
        order_id = order.id
        subscription = session.exec(
            select(Subscription).where(Subscription.order_id == order_id)
        ).one()
        subscription.status = SubscriptionStatus.CANCELED
        session.add(subscription)
        session.commit()

    # Late webhook arrives claiming recurring charge succeeded
    period_payload = ecpay_period_payload(order_no)
    response = client.post("/callbacks/ecpay/period", data=period_payload)

    assert response.text == "1|OK"
    with Session(app.state.engine) as session:
        subscription = session.exec(
            select(Subscription).where(Subscription.order_id == order_id)
        ).one()
        # Invariant: status must remain CANCELED
        assert subscription.status == SubscriptionStatus.CANCELED
        audit = session.exec(
            select(PaymentNotification).where(
                PaymentNotification.order_no == order_no,
                PaymentNotification.kind == NotificationKind.RECURRING_PAYMENT,
            )
        ).one()
        assert audit.processed_result == ProcessedResult.IGNORED_TRANSITION


# =============================================================================
# 2. Failure-Injection & Transaction Atomicity Tests
# =============================================================================


def test_failure_injection_in_payment_notification_rolls_back_cleanly(
    client, app, monkeypatch
):
    """Failure injection: when DB commit fails during notification handling,
    the entire transaction is rolled back and neither Order nor applied row remains."""
    order_no = place_order(client, "ecpay", "credit_card")
    payload = ecpay_payment_payload(order_no)

    original_commit = Session.commit

    def faulty_commit(self):
        # Simulate an unexpected database error during commit
        raise RuntimeError("Injected DB failure during commit")

    monkeypatch.setattr(Session, "commit", faulty_commit)

    with pytest.raises(RuntimeError, match="Injected DB failure"):
        client.post("/callbacks/ecpay/notify", data=payload)

    # Restore original commit and verify state in DB
    monkeypatch.setattr(Session, "commit", original_commit)

    assert order_status(client, order_no) == "pending"
    with Session(app.state.engine) as session:
        applied_rows = session.exec(
            select(PaymentNotification).where(
                PaymentNotification.order_no == order_no,
                PaymentNotification.processed_result == ProcessedResult.APPLIED,
            )
        ).all()
        assert len(applied_rows) == 0


def test_failure_injection_in_recurring_notification_rolls_back_cleanly(
    client, app, monkeypatch
):
    """Failure injection in recurring webhook: rollback guarantees neither
    charge ledger row nor subscription counters are half-updated."""
    order_no = place_subscription(client)
    client.post("/callbacks/ecpay/notify", data=ecpay_payment_payload(order_no))

    period_payload = ecpay_period_payload(order_no)

    original_commit = Session.commit

    def faulty_commit(self):
        raise RuntimeError("Injected DB failure in recurring commit")

    monkeypatch.setattr(Session, "commit", faulty_commit)

    with pytest.raises(RuntimeError, match="Injected DB failure"):
        client.post("/callbacks/ecpay/period", data=period_payload)

    monkeypatch.setattr(Session, "commit", original_commit)

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
        # Subscription should retain initial active state with 0 recurring charge rows
        assert subscription.success_times == 1
        assert len(charges) == 0


# =============================================================================
# 3. Idempotency & Replay Safety Tests
# =============================================================================


def test_duplicate_trade_no_with_different_order_no_is_rejected(client):
    """If a notification reuses an applied TradeNo but attaches a different OrderNo,
    it must be rejected rather than silently acknowledged as a valid duplicate."""
    order_no_1 = place_order(client, "ecpay", "credit_card")
    order_no_2 = place_order(client, "ecpay", "credit_card")

    payload_1 = ecpay_payment_payload(order_no_1, TradeNo="EC_SHARED_TRADE_01")
    first = client.post("/callbacks/ecpay/notify", data=payload_1)
    assert first.text == "1|OK"
    assert order_status(client, order_no_1) == "paid"

    # Conflicting replay: same TradeNo, different OrderNo
    payload_conflicting = ecpay_payment_payload(
        order_no_2, TradeNo="EC_SHARED_TRADE_01"
    )
    second = client.post("/callbacks/ecpay/notify", data=payload_conflicting)

    assert second.text.startswith("0|")
    assert order_status(client, order_no_2) == "pending"


def test_refund_simulation_idempotency_conflict_detection(app):
    """Reusing an idempotency key with conflicting parameters must reject the request."""
    with Session(app.state.engine) as session:
        order = Order(
            order_no="T9999999999990001",
            product_id=1,
            amount=500,
            status=OrderStatus.PAID,
            gateway=GatewayName.ECPAY,
            payment_method=PaymentMethod.CREDIT_CARD,
        )
        session.add(order)
        session.commit()
        session.refresh(order)

        # First refund request
        first = simulate_refund(
            session,
            order,
            amount=200,
            action=RefundAction.REFUND,
            reason="Customer return",
            idempotency_key="refund-key-xyz",
        )
        assert first.status == RefundStatus.SIMULATED_SUCCEEDED

        # Idempotent replay: exact same parameters -> returns existing
        same_replay = simulate_refund(
            session,
            order,
            amount=200,
            action=RefundAction.REFUND,
            reason="Customer return",
            idempotency_key="refund-key-xyz",
        )
        assert same_replay.id == first.id
        assert same_replay.status == RefundStatus.SIMULATED_SUCCEEDED

        # Conflicting request: same key, different amount -> rejected
        conflict = simulate_refund(
            session,
            order,
            amount=300,
            action=RefundAction.REFUND,
            reason="Customer return",
            idempotency_key="refund-key-xyz",
        )
        assert conflict.status == RefundStatus.REJECTED
        assert "Idempotency conflict" in conflict.gateway_message


# =============================================================================
# 4. DB-Level Constraints Enforcement Tests
# =============================================================================


def test_db_constraint_rejects_negative_order_amount(app):
    """DB check constraint ck_orders_amount_positive must reject non-positive amounts."""
    with Session(app.state.engine) as session:
        bad_order = Order(
            order_no="T000000000000BAD1",
            product_id=1,
            amount=-100,
            status=OrderStatus.PENDING,
            gateway=GatewayName.ECPAY,
            payment_method=PaymentMethod.CREDIT_CARD,
        )
        session.add(bad_order)
        with pytest.raises(IntegrityError):
            session.commit()


def test_db_constraint_rejects_negative_refund_amount(app):
    """DB check constraint ck_refund_amount_positive must reject non-positive amounts."""
    with Session(app.state.engine) as session:
        bad_refund = RefundRequest(
            order_id=1,
            idempotency_key="bad-refund-key",
            action=RefundAction.REFUND,
            amount=0,
            status=RefundStatus.SIMULATED_SUCCEEDED,
        )
        session.add(bad_refund)
        with pytest.raises(IntegrityError):
            session.commit()


def test_db_constraint_rejects_duplicate_reconciliation_order_in_same_run(app):
    """UniqueConstraint on (run_id, order_no) must reject duplicate item rows per run."""
    with Session(app.state.engine) as session:
        item1 = ReconciliationItem(
            run_id=100,
            order_no="TORDER0000000001",
            result=ReconciliationResult.MATCHED,
        )
        item2 = ReconciliationItem(
            run_id=100,
            order_no="TORDER0000000001",
            result=ReconciliationResult.MATCHED,
        )
        session.add(item1)
        session.commit()
        session.add(item2)
        with pytest.raises(IntegrityError):
            session.commit()


# =============================================================================
# 5. Query Recovery Synchronizes Subscription Atomically
# =============================================================================


def test_query_recovery_synchronizes_attached_subscription(client, app):
    """When query recovery marks a recurring initial order PAID, its attached
    subscription is activated atomically in the same transaction."""
    order_no = place_subscription(client)
    app.state.ecpay_form_transport = lambda _url, _fields: query_response(order_no)

    response = client.post(
        f"/admin/orders/{order_no}/query", follow_redirects=False
    )
    assert response.status_code == 303
    assert "query=recovered" in response.headers["location"]
    assert order_status(client, order_no) == "paid"

    with Session(app.state.engine) as session:
        order = session.exec(select(Order).where(Order.order_no == order_no)).one()
        subscription = session.exec(
            select(Subscription).where(Subscription.order_id == order.id)
        ).one()
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.success_times == 1
        assert subscription.success_amount == order.amount
