"""Engine/session helpers and initial seed data."""
from sqlalchemy import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from .models import Product

# 用純資料宣告，於 init_db 內建立新實例——模組層級的 model 實例
# 被某個 engine seed 過後會帶著主鍵，無法再用於其他 engine（測試會踩到）。
SEED_PRODUCTS: list[dict] = [
    {"name": "手沖咖啡豆 250g", "price": 450, "description": "中深焙，示範商品"},
    {"name": "曲奇餅乾禮盒", "price": 120, "description": "十二入，示範商品"},
    {"name": "不鏽鋼保溫瓶", "price": 890, "description": "500ml，示範商品"},
]


def build_engine(database_url: str) -> Engine:
    """Create the SQLAlchemy engine; in-memory SQLite shares one connection for tests."""
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if ":memory:" in database_url:
        kwargs["poolclass"] = StaticPool
    return create_engine(database_url, **kwargs)


def init_db(engine: Engine) -> None:
    """Create tables and seed the three demo products (idempotent)."""
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        if session.exec(select(Product)).first() is None:
            for fields in SEED_PRODUCTS:
                session.add(Product(**fields))
            session.commit()
