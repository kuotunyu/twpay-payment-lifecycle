"""Database models for checkout, payment operations, and immutable audit trails."""
from datetime import date, datetime
from enum import Enum

from sqlalchemy import CheckConstraint, Column, Index, UniqueConstraint, text
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel


class GatewayName(str, Enum):
    ECPAY = "ecpay"
    NEWEBPAY = "newebpay"


class PaymentMethod(str, Enum):
    CREDIT_CARD = "credit_card"
    ATM = "atm"


class OrderStatus(str, Enum):
    PENDING = "pending"
    AWAITING_PAYMENT = "awaiting_payment"  # ATM 已取號，等待入帳
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"


class NotificationKind(str, Enum):
    PAYMENT_RESULT = "payment_result"
    ATM_ACCOUNT_ISSUED = "atm_account_issued"
    RECURRING_PAYMENT = "recurring_payment"


class ProcessedResult(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    REJECTED_SIGNATURE = "rejected_signature"
    REJECTED_AMOUNT = "rejected_amount"
    UNKNOWN_ORDER = "unknown_order"
    IGNORED_TRANSITION = "ignored_transition"


class SubscriptionStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    COMPLETED = "completed"
    CANCELED = "canceled"


class SubscriptionAction(str, Enum):
    CANCEL = "Cancel"
    REAUTHORIZE = "ReAuth"


class SubscriptionActionStatus(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    GATEWAY_ERROR = "gateway_error"


class SubscriptionQueryOutcome(str, Enum):
    SYNCED = "synced"
    REJECTED = "rejected"
    GATEWAY_ERROR = "gateway_error"


class QueryOutcome(str, Enum):
    RECOVERED = "recovered"
    CONFIRMED_PAID = "confirmed_paid"
    UNPAID = "unpaid"
    REJECTED_SIGNATURE = "rejected_signature"
    REJECTED_ORDER = "rejected_order"
    REJECTED_AMOUNT = "rejected_amount"
    GATEWAY_ERROR = "gateway_error"


class RefundAction(str, Enum):
    VOID = "void"
    REFUND = "refund"


class RefundStatus(str, Enum):
    SIMULATED_SUCCEEDED = "simulated_succeeded"
    REJECTED = "rejected"


class ReconciliationStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class ReconciliationResult(str, Enum):
    MATCHED = "matched"
    MISSING_LOCAL = "missing_local"
    MISSING_GATEWAY = "missing_gateway"
    AMOUNT_MISMATCH = "amount_mismatch"
    STATUS_MISMATCH = "status_mismatch"


def _enum_col(enum_cls: type[Enum], **kwargs) -> Column:
    """列舉欄位一律以「值」（小寫）落庫。

    SQLAlchemy 預設存列舉「名稱」（如 'APPLIED'），會讓下方部分唯一索引的
    WHERE 條件永遠比對不到，DB 層冪等防線形同虛設——以值落庫讓字面值一致。
    """
    return Column(
        SAEnum(
            enum_cls,
            values_callable=lambda e: [member.value for member in e],
            native_enum=False,
            validate_strings=True,
        ),
        **kwargs,
    )


class Product(SQLModel, table=True):
    __tablename__ = "product"
    __table_args__ = (
        CheckConstraint("price > 0", name="ck_product_price_positive"),
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str
    price: int  # 新台幣整數元，金額不使用浮點數
    description: str = ""


class Order(SQLModel, table=True):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_orders_amount_positive"),
    )

    id: int | None = Field(default=None, primary_key=True)
    order_no: str = Field(unique=True, index=True)  # 兩家金流的交易編號都用它
    product_id: int = Field(foreign_key="product.id")
    amount: int  # 建單時從 Product.price 複製；通知入帳前必比對
    status: OrderStatus = Field(
        default=OrderStatus.PENDING, sa_column=_enum_col(OrderStatus, nullable=False)
    )
    gateway: GatewayName = Field(sa_column=_enum_col(GatewayName, nullable=False))
    payment_method: PaymentMethod = Field(
        sa_column=_enum_col(PaymentMethod, nullable=False)
    )
    gateway_trade_no: str | None = None  # 金流商交易編號（TradeNo）
    # ATM 取號結果
    bank_code: str | None = None
    virtual_account: str | None = None
    pay_deadline: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    paid_at: datetime | None = None


class PaymentNotification(SQLModel, table=True):
    """每一筆進站通知的原始紀錄（含驗簽失敗者），供冪等判斷與稽核。

    冪等由部分唯一索引保證：同一 (gateway, gateway_trade_no, kind) 最多只有
    一列 processed_result='applied'；其餘結果（重複、拒絕…）每次都留下稽核列。
    """

    __tablename__ = "payment_notification"
    __table_args__ = (
        Index(
            "uq_applied_notification",
            "gateway",
            "gateway_trade_no",
            "kind",
            unique=True,
            sqlite_where=text("processed_result = 'applied'"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    gateway: GatewayName = Field(sa_column=_enum_col(GatewayName, nullable=False))
    order_no: str | None = None
    gateway_trade_no: str | None = None  # 驗簽失敗時內容不可信，留 None
    kind: NotificationKind = Field(sa_column=_enum_col(NotificationKind, nullable=False))
    raw_payload: str  # 原始 form 內容（urlencoded）
    signature_valid: bool
    amount_matched: bool | None = None
    processed_result: ProcessedResult = Field(
        sa_column=_enum_col(ProcessedResult, nullable=False)
    )
    received_at: datetime = Field(default_factory=datetime.now)


class Subscription(SQLModel, table=True):
    """Recurring plan attached to the initial ECPay order."""

    __tablename__ = "subscription"
    __table_args__ = (
        CheckConstraint("frequency >= 1", name="ck_subscription_frequency_positive"),
        CheckConstraint("exec_times >= 1", name="ck_subscription_exec_times_positive"),
        CheckConstraint(
            "success_times >= 0", name="ck_subscription_success_times_non_negative"
        ),
        CheckConstraint(
            "failure_times >= 0", name="ck_subscription_failure_times_non_negative"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", unique=True, index=True)
    period_type: str = "M"
    frequency: int = 1
    exec_times: int = 3
    status: SubscriptionStatus = Field(
        default=SubscriptionStatus.PENDING,
        sa_column=_enum_col(SubscriptionStatus, nullable=False),
    )
    success_times: int = 0
    success_amount: int = 0
    failure_times: int = 0
    last_trade_no: str | None = None
    last_message: str | None = None
    last_charged_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class SubscriptionCharge(SQLModel, table=True):
    """One immutable recurring authorization attempt."""

    __tablename__ = "subscription_charge"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_subscription_charge_amount_positive"),
        CheckConstraint(
            "sequence >= 1", name="ck_subscription_charge_sequence_positive"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    subscription_id: int = Field(foreign_key="subscription.id", index=True)
    gateway_trade_no: str = Field(unique=True, index=True)
    sequence: int
    amount: int
    success: bool
    signature_valid: bool = True
    amount_matched: bool
    message: str = ""
    raw_payload: str
    charged_at: datetime = Field(default_factory=datetime.now)


class SubscriptionActionLog(SQLModel, table=True):
    """Immutable audit record for an ECPay recurring-order operation."""

    __tablename__ = "subscription_action_log"

    id: int | None = Field(default=None, primary_key=True)
    subscription_id: int = Field(foreign_key="subscription.id", index=True)
    action: SubscriptionAction = Field(
        sa_column=_enum_col(SubscriptionAction, nullable=False)
    )
    status: SubscriptionActionStatus = Field(
        sa_column=_enum_col(SubscriptionActionStatus, nullable=False)
    )
    signature_valid: bool = False
    gateway_code: str | None = None
    gateway_message: str = ""
    raw_payload: str
    created_at: datetime = Field(default_factory=datetime.now)


class SubscriptionQueryLog(SQLModel, table=True):
    """Audit for recurring-order detail queries and missed-period recovery."""

    __tablename__ = "subscription_query_log"

    id: int | None = Field(default=None, primary_key=True)
    subscription_id: int = Field(foreign_key="subscription.id", index=True)
    outcome: SubscriptionQueryOutcome = Field(
        sa_column=_enum_col(SubscriptionQueryOutcome, nullable=False)
    )
    order_matched: bool = False
    amount_matched: bool = False
    schedule_matched: bool = False
    remote_exec_status: str | None = None
    remote_success_times: int | None = None
    recovered_charges: int = 0
    gateway_message: str = ""
    raw_payload: str
    created_at: datetime = Field(default_factory=datetime.now)


class GatewayQueryLog(SQLModel, table=True):
    """Immutable log for every proactive ECPay order query."""

    __tablename__ = "gateway_query_log"

    id: int | None = Field(default=None, primary_key=True)
    order_no: str = Field(index=True)
    gateway_trade_no: str | None = None
    remote_status: str | None = None
    remote_amount: int | None = None
    signature_valid: bool = False
    order_matched: bool = False
    amount_matched: bool = False
    outcome: QueryOutcome = Field(sa_column=_enum_col(QueryOutcome, nullable=False))
    raw_payload: str
    queried_at: datetime = Field(default_factory=datetime.now)


class RefundRequest(SQLModel, table=True):
    """Audited sandbox refund/void simulation.

    ECPay explicitly provides no test endpoint for the credit-card action API,
    so this table never claims a live gateway refund.
    """

    __tablename__ = "refund_request"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_refund_amount_positive"),
    )

    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    idempotency_key: str = Field(unique=True, index=True)
    action: RefundAction = Field(sa_column=_enum_col(RefundAction, nullable=False))
    amount: int
    reason: str = ""
    status: RefundStatus = Field(sa_column=_enum_col(RefundStatus, nullable=False))
    mode: str = "sandbox_simulation"
    gateway_message: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class ReconciliationRun(SQLModel, table=True):
    __tablename__ = "reconciliation_run"

    id: int | None = Field(default=None, primary_key=True)
    period_start: date
    period_end: date
    source_name: str
    status: ReconciliationStatus = Field(
        sa_column=_enum_col(ReconciliationStatus, nullable=False)
    )
    gateway_rows: int = 0
    local_rows: int = 0
    matched_rows: int = 0
    difference_rows: int = 0
    error_message: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class ReconciliationItem(SQLModel, table=True):
    __tablename__ = "reconciliation_item"
    __table_args__ = (
        UniqueConstraint("run_id", "order_no", name="uq_reconciliation_item_run_order"),
    )

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="reconciliation_run.id", index=True)
    order_no: str
    gateway_trade_no: str | None = None
    local_amount: int | None = None
    gateway_amount: int | None = None
    local_status: str | None = None
    gateway_status: str | None = None
    result: ReconciliationResult = Field(
        sa_column=_enum_col(ReconciliationResult, nullable=False)
    )
