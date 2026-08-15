"""State transition invariants and lifecycle helpers for orders and subscriptions."""
from datetime import datetime

from ..models import NotificationKind, Order, OrderStatus, Subscription, SubscriptionStatus

# Order lifecycle transitions:
# PENDING -> AWAITING_PAYMENT (ATM virtual account issued)
# PENDING -> PAID (Card payment or direct payment success)
# PENDING -> FAILED (Payment failure)
# AWAITING_PAYMENT -> PAID (ATM payment success)
# AWAITING_PAYMENT -> FAILED (ATM payment failure / cancel)
# EXPIRED (derived from AWAITING_PAYMENT at read time) -> PAID (Bank received payment late)
# PAID -> Terminal (no transitions permitted)
# FAILED -> Terminal (no transitions permitted)
VALID_ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {
        OrderStatus.AWAITING_PAYMENT,
        OrderStatus.PAID,
        OrderStatus.FAILED,
    },
    OrderStatus.AWAITING_PAYMENT: {
        OrderStatus.PAID,
        OrderStatus.FAILED,
    },
    OrderStatus.EXPIRED: {
        OrderStatus.PAID,
    },
    OrderStatus.PAID: set(),
    OrderStatus.FAILED: set(),
}

# Subscription lifecycle transitions:
# PENDING -> ACTIVE (Initial checkout order paid)
# PENDING -> CANCELED (Direct cancellation if applicable)
# ACTIVE -> ACTIVE (Recurring authorization succeeded, exec_times not reached)
# ACTIVE -> COMPLETED (Final recurring authorization succeeded)
# ACTIVE -> PAST_DUE (Recurring authorization failed)
# ACTIVE -> CANCELED (Operator manual cancellation)
# PAST_DUE -> ACTIVE (Subsequent charge succeeded, exec_times not reached)
# PAST_DUE -> COMPLETED (Subsequent charge succeeded and reached exec_times)
# PAST_DUE -> PAST_DUE (Another recurring attempt failed)
# PAST_DUE -> CANCELED (Operator manual cancellation)
# COMPLETED -> Terminal (Late webhooks must not demote to PAST_DUE or ACTIVE)
# CANCELED -> Terminal (Late webhooks must not reactivate or demote to PAST_DUE)
VALID_SUBSCRIPTION_TRANSITIONS: dict[SubscriptionStatus, set[SubscriptionStatus]] = {
    SubscriptionStatus.PENDING: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.CANCELED,
    },
    SubscriptionStatus.ACTIVE: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.COMPLETED,
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.CANCELED,
    },
    SubscriptionStatus.PAST_DUE: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.COMPLETED,
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.CANCELED,
    },
    SubscriptionStatus.COMPLETED: set(),
    SubscriptionStatus.CANCELED: set(),
}


def can_transition_order(current: OrderStatus, target: OrderStatus) -> bool:
    """Check if an order status transition is permitted by domain invariants."""
    return target in VALID_ORDER_TRANSITIONS.get(current, set())


def can_transition_subscription(
    current: SubscriptionStatus, target: SubscriptionStatus
) -> bool:
    """Check if a subscription status transition is permitted by domain invariants."""
    return target in VALID_SUBSCRIPTION_TRANSITIONS.get(current, set())


def apply_order_transition(
    order: Order,
    *,
    success: bool,
    gateway_trade_no: str | None,
    kind: NotificationKind,
    paid_at: datetime | None = None,
    atm_info=None,
) -> bool:
    """Attempt an atomic state mutation on an Order based on verified notification data.

    Returns True if the transition was applied, or False if the transition is illegal/ignored.
    """
    if kind == NotificationKind.ATM_ACCOUNT_ISSUED:
        if (
            success
            and atm_info is not None
            and can_transition_order(order.status, OrderStatus.AWAITING_PAYMENT)
        ):
            order.status = OrderStatus.AWAITING_PAYMENT
            order.gateway_trade_no = gateway_trade_no
            order.bank_code = atm_info.bank_code
            order.virtual_account = atm_info.virtual_account
            order.pay_deadline = atm_info.pay_deadline
            return True
        return False

    target_status = OrderStatus.PAID if success else OrderStatus.FAILED
    if can_transition_order(order.status, target_status):
        order.gateway_trade_no = gateway_trade_no
        order.status = target_status
        if target_status == OrderStatus.PAID:
            order.paid_at = paid_at or datetime.now()
        return True
    return False


def apply_subscription_recurring_transition(
    subscription: Subscription,
    *,
    success: bool,
    total_times: int,
    total_amount: int,
    gateway_trade_no: str,
    message: str = "",
    charged_at: datetime | None = None,
) -> bool:
    """Apply recurring charge outcome to Subscription while respecting terminal state invariants.

    Returns True if subscription status and counters were updated, or False if subscription
    is in a terminal state (CANCELED or COMPLETED) that rejects further status mutations.
    """
    now = datetime.now()
    charged_timestamp = charged_at or now

    if success:
        target_status = (
            SubscriptionStatus.COMPLETED
            if total_times >= subscription.exec_times
            else SubscriptionStatus.ACTIVE
        )
    else:
        target_status = SubscriptionStatus.PAST_DUE

    if not can_transition_subscription(subscription.status, target_status):
        # Terminal state invariant: do not reactivate or demote CANCELED or COMPLETED plans
        return False

    if success:
        subscription.success_times = max(subscription.success_times, total_times)
        subscription.success_amount = max(subscription.success_amount, total_amount)
    else:
        subscription.failure_times += 1

    subscription.status = target_status
    subscription.last_trade_no = gateway_trade_no
    subscription.last_message = message
    subscription.last_charged_at = charged_timestamp
    subscription.updated_at = now
    return True
