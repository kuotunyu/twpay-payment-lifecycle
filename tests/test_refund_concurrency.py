"""simulate_refund 的併發回歸測試：檔案型 SQLite、各自獨立的 Session／交易。

這些案例刻意不用 conftest 的 in-memory fixture——`sqlite:///:memory:` 走 StaticPool，
全部 Session 共用同一條連線，結構上就不可能重現跨請求（deps.get_session 每個請求一個
Session）的競態。這裡改用 tmp_path 下的檔案型 DB，與正式預設 `sqlite:///twpay.db` 同構。

決定性來自 _Gate，而不是 sleep 或時序運氣：兩個工作執行緒都必須先抵達觀測點，閘門才放行。
觀測點刻意設兩個——
  * 讀取「既有成功退款」之後（修好前、修好後都會執行到）；
  * 送出 BEGIN IMMEDIATE 之前（只有修好後才有，且在真正拿到寫鎖之前就先報到）。
修好前兩執行緒都在第一個觀測點會合，讀到同一份過期餘額；修好後落後的那一方在被寫鎖擋住
「之前」就已報到，先行者立刻被放行、提交並釋放鎖——臨界區內不會有人等待對方，不會死鎖。
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from twpay_checkout.db import build_engine
from twpay_checkout.models import (
    GatewayName,
    Order,
    OrderStatus,
    PaymentMethod,
    RefundAction,
    RefundRequest,
    RefundStatus,
)
from twpay_checkout.services.operations import simulate_refund

ORDER_AMOUNT = 500
ORDER_NO = "T9999999999990001"
GATE_TIMEOUT = 20.0  # 安全網，不是同步機制本身
JOIN_TIMEOUT = 30.0

# 餘額讀取：SELECT ... WHERE refund_request.order_id = ? AND refund_request.status = ?
REMAINING_READ_MARKER = "WHERE refund_request.order_id"
WRITE_LOCK_MARKER = "BEGIN IMMEDIATE"


class _Gate:
    """兩方會合點：雙方都抵達「任一」觀測點後才放行。

    與 threading.Barrier 的差別是報到點與等待點可以分離——落後者在即將被資料庫寫鎖
    擋住之前先報到（wait=False），先行者因此能立刻離開臨界區，避免把兩方屏障放進
    被序列化的區段而互鎖。
    """

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._condition = threading.Condition()
        self._arrived: set[str] = set()

    def arrive(self, *, wait: bool) -> None:
        with self._condition:
            self._arrived.add(threading.current_thread().name)
            if len(self._arrived) >= self._parties:
                self._condition.notify_all()
                return
            if wait:
                self._condition.wait_for(
                    lambda: len(self._arrived) >= self._parties, timeout=GATE_TIMEOUT
                )


@pytest.fixture()
def refund_db(tmp_path):
    """檔案型 SQLite + 一筆已付款的 ECPay 信用卡訂單（NT$500）。"""
    engine = build_engine(f"sqlite:///{tmp_path / 'refund.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Order(
                order_no=ORDER_NO,
                product_id=1,
                amount=ORDER_AMOUNT,
                status=OrderStatus.PAID,
                gateway=GatewayName.ECPAY,
                payment_method=PaymentMethod.CREDIT_CARD,
            )
        )
        session.commit()
        order_id = session.exec(select(Order.id).where(Order.order_no == ORDER_NO)).one()
    yield engine, order_id
    engine.dispose()


def _install_gate(engine, parties: int = 2) -> _Gate:
    gate = _Gate(parties)

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(WRITE_LOCK_MARKER):
            gate.arrive(wait=False)

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        if REMAINING_READ_MARKER in statement:
            gate.arrive(wait=True)

    return gate


def _run_concurrently(engine, order_id: int, calls: dict[str, dict]) -> dict[str, tuple]:
    """同時跑兩次 simulate_refund，每個執行緒一個獨立 Session（等同兩個 HTTP 請求）。"""
    _install_gate(engine, parties=len(calls))
    results: dict[str, tuple] = {}

    def worker(name: str, kwargs: dict) -> None:
        try:
            with Session(engine) as session:
                order = session.get(Order, order_id)
                row = simulate_refund(session, order, **kwargs)
                results[name] = ("ok", row.id, row.status, row.amount, row.gateway_message)
        except Exception as exc:  # noqa: BLE001 - 逃逸的例外本身就是待測行為
            results[name] = ("error", type(exc).__name__, str(exc))

    threads = [
        threading.Thread(target=worker, args=(name, kwargs), name=name)
        for name, kwargs in calls.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT)
    for thread in threads:
        assert not thread.is_alive(), f"worker {thread.name} 未在 {JOIN_TIMEOUT}s 內結束"
    assert set(results) == set(calls), "有工作執行緒沒有留下結果"
    return results


def _succeeded(engine) -> list[RefundRequest]:
    with Session(engine) as session:
        return [
            row
            for row in session.exec(select(RefundRequest)).all()
            if row.status == RefundStatus.SIMULATED_SUCCEEDED
        ]


def _all_rows(engine) -> list[RefundRequest]:
    with Session(engine) as session:
        return list(session.exec(select(RefundRequest)).all())


def test_concurrent_distinct_keys_cannot_exceed_order_amount(refund_db):
    """不同冪等鍵併發：成功的模擬退款總額不得超過訂單金額。"""
    engine, order_id = refund_db
    base = {"action": RefundAction.REFUND, "reason": "concurrent full refund"}
    results = _run_concurrently(
        engine,
        order_id,
        {
            "A": {**base, "amount": ORDER_AMOUNT, "idempotency_key": "key-A"},
            "B": {**base, "amount": ORDER_AMOUNT, "idempotency_key": "key-B"},
        },
    )

    for name, outcome in results.items():
        assert outcome[0] == "ok", f"{name} 逃出未受控例外：{outcome}"

    succeeded = _succeeded(engine)
    total = sum(row.amount for row in succeeded)
    assert total <= ORDER_AMOUNT, f"超額退款：{total} > {ORDER_AMOUNT}"
    assert len(succeeded) == 1
    rejected = [row for row in _all_rows(engine) if row.status == RefundStatus.REJECTED]
    assert len(rejected) == 1
    assert "超過剩餘可退金額" in rejected[0].gateway_message


def test_concurrent_same_key_replay_returns_the_winning_row(refund_db):
    """相同冪等鍵併發重放：不得重複生效，也不得丟出未受控例外。"""
    engine, order_id = refund_db
    call = {
        "amount": 200,
        "action": RefundAction.REFUND,
        "reason": "same-key replay",
        "idempotency_key": "refund-key-shared",
    }
    results = _run_concurrently(engine, order_id, {"A": dict(call), "B": dict(call)})

    for name, outcome in results.items():
        assert outcome[0] == "ok", f"{name} 逃出未受控例外：{outcome}"
    assert results["A"][1] == results["B"][1], "兩次重放應回到同一列"
    assert results["A"][2] == RefundStatus.SIMULATED_SUCCEEDED

    rows = _all_rows(engine)
    assert len(rows) == 1
    assert rows[0].amount == 200
    assert rows[0].status == RefundStatus.SIMULATED_SUCCEEDED


def test_concurrent_same_key_conflicting_payload_is_rejected_in_a_controlled_way(refund_db):
    """相同冪等鍵但參數不同併發：落後者要拿到受控的衝突拒絕，而不是 IntegrityError。"""
    engine, order_id = refund_db
    base = {
        "action": RefundAction.REFUND,
        "reason": "conflicting payload",
        "idempotency_key": "refund-key-conflict",
    }
    results = _run_concurrently(
        engine,
        order_id,
        {"A": {**base, "amount": 200}, "B": {**base, "amount": 300}},
    )

    for name, outcome in results.items():
        assert outcome[0] == "ok", f"{name} 逃出未受控例外：{outcome}"

    rows = _all_rows(engine)
    succeeded = [row for row in rows if row.status == RefundStatus.SIMULATED_SUCCEEDED]
    rejected = [row for row in rows if row.status == RefundStatus.REJECTED]
    assert len(succeeded) == 1
    assert len(rejected) == 1
    assert "Idempotency conflict" in rejected[0].gateway_message
    assert sum(row.amount for row in succeeded) <= ORDER_AMOUNT


def test_failed_refund_rolls_back_and_leaves_the_session_usable(refund_db):
    """寫入失敗時必須回滾：不留半套效果，呼叫端的 Session 仍可繼續使用。"""
    engine, order_id = refund_db
    with Session(engine) as session:
        order = session.get(Order, order_id)
        # amount <= 0 會先被政策擋下成 REJECTED，但該列仍帶 amount=0，
        # 因而撞上 ck_refund_amount_positive——正好用來驗證失敗後的回滾路徑。
        with pytest.raises(IntegrityError):
            simulate_refund(
                session,
                order,
                amount=0,
                action=RefundAction.REFUND,
                reason="zero amount",
                idempotency_key="zero-amount-key",
            )

        assert session.exec(select(RefundRequest)).all() == [], "失敗不應留下任何列"

        recovered = simulate_refund(
            session,
            order,
            amount=100,
            action=RefundAction.REFUND,
            reason="after rollback",
            idempotency_key="after-rollback-key",
        )
        assert recovered.status == RefundStatus.SIMULATED_SUCCEEDED


def _driver_in_transaction(session: Session) -> bool:
    """呼叫端 Session 底下的 sqlite3 連線是否仍掛著交易（含未釋放的 BEGIN IMMEDIATE）。"""
    return session.connection().connection.driver_connection.in_transaction


def test_lookup_failure_does_not_leak_the_owned_write_transaction(refund_db):
    """取得寫鎖之後、提交之前拋出的例外，必須回滾本函式自己開的交易。"""
    engine, order_id = refund_db
    with Session(engine) as session:
        order = session.get(Order, order_id)

        @event.listens_for(engine, "before_cursor_execute")
        def _explode(conn, cursor, statement, parameters, context, executemany):
            if "FROM refund_request" in statement:
                raise RuntimeError("injected lookup failure")

        try:
            with pytest.raises(RuntimeError, match="injected lookup failure"):
                simulate_refund(
                    session,
                    order,
                    amount=100,
                    action=RefundAction.REFUND,
                    reason="lookup blows up",
                    idempotency_key="lookup-failure-key",
                )
        finally:
            event.remove(engine, "before_cursor_execute", _explode)

        assert not _driver_in_transaction(session), "寫鎖沒有釋放，交易洩漏到呼叫端"
        assert session.exec(select(RefundRequest)).all() == []
        recovered = simulate_refund(
            session,
            order,
            amount=100,
            action=RefundAction.REFUND,
            reason="after lookup failure",
            idempotency_key="after-lookup-failure-key",
        )
        assert recovered.status == RefundStatus.SIMULATED_SUCCEEDED


def test_replay_commit_failure_does_not_leak_the_owned_write_transaction(refund_db):
    """純重放路徑的提交失敗同樣要回滾——該路徑沒有自己的寫入，但仍持有寫鎖。"""
    engine, order_id = refund_db
    key = "replay-commit-failure-key"
    with Session(engine) as session:
        order = session.get(Order, order_id)
        first = simulate_refund(
            session,
            order,
            amount=100,
            action=RefundAction.REFUND,
            reason="seed the replay",
            idempotency_key=key,
        )
        assert first.status == RefundStatus.SIMULATED_SUCCEEDED

        original_commit = session.commit

        def failing_commit() -> None:
            raise RuntimeError("injected commit failure")

        session.commit = failing_commit
        try:
            with pytest.raises(RuntimeError, match="injected commit failure"):
                simulate_refund(
                    session,
                    order,
                    amount=100,
                    action=RefundAction.REFUND,
                    reason="seed the replay",
                    idempotency_key=key,
                )
        finally:
            session.commit = original_commit

        assert not _driver_in_transaction(session), "重放路徑把寫鎖留在交易裡"
        assert len(session.exec(select(RefundRequest)).all()) == 1


def test_refund_refuses_a_session_with_unflushed_pending_work(refund_db):
    """呼叫端還有未 flush 的待處理物件時就要拒絕——否則 autoflush 會把對方的工作
    捲進本函式的交易，隨著提交一起生效或隨著回滾一起消失。"""
    engine, order_id = refund_db
    with Session(engine) as session:
        order = session.get(Order, order_id)
        pending = Order(
            order_no="T9999999999990098",
            product_id=1,
            amount=130,
            status=OrderStatus.PENDING,
            gateway=GatewayName.ECPAY,
            payment_method=PaymentMethod.CREDIT_CARD,
        )
        session.add(pending)  # 只 add，不 flush

        with pytest.raises(RuntimeError, match="unflushed pending work"):
            simulate_refund(
                session,
                order,
                amount=100,
                action=RefundAction.REFUND,
                reason="caller has pending work",
                idempotency_key="pending-work-key",
            )

        assert pending in session.new, "呼叫端未 flush 的工作被動到了"
        assert not _driver_in_transaction(session)
        assert session.exec(select(RefundRequest)).all() == []

        # 呼叫端仍可自行決定提交；工作完好無缺。
        session.commit()
        assert (
            session.exec(
                select(Order).where(Order.order_no == "T9999999999990098")
            ).first()
            is not None
        )


def test_refund_refuses_a_session_that_already_holds_a_write_transaction(refund_db):
    """呼叫端若有未提交的寫入，必須明確拒絕，且不得偷偷回滾對方的工作。"""
    engine, order_id = refund_db
    with Session(engine) as session:
        order = session.get(Order, order_id)
        session.add(
            Order(
                order_no="T9999999999990099",
                product_id=1,
                amount=120,
                status=OrderStatus.PENDING,
                gateway=GatewayName.ECPAY,
                payment_method=PaymentMethod.CREDIT_CARD,
            )
        )
        session.flush()  # 呼叫端自己的、尚未提交的寫入

        with pytest.raises(RuntimeError, match="open write transaction"):
            simulate_refund(
                session,
                order,
                amount=100,
                action=RefundAction.REFUND,
                reason="dirty caller session",
                idempotency_key="dirty-session-key",
            )

        caller_work = session.exec(
            select(Order).where(Order.order_no == "T9999999999990099")
        ).first()
        assert caller_work is not None, "不得代為回滾呼叫端的工作"
        assert session.exec(select(RefundRequest)).all() == []
        session.rollback()
