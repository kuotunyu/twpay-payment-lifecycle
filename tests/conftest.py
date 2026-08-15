"""Shared test fixtures: in-memory app with the official example keys.

測試金鑰即官方文件範例金鑰（綠界 3002607／藍新 NDNF-1.2.3 範例），
整合測試用自家簽章函式造 payload——簽章函式本身已由官方範例值單元測試釘死。
"""
import pytest
from fastapi.testclient import TestClient

from twpay_checkout.config import Settings
from twpay_checkout.db import init_db
from twpay_checkout.main import create_app

ECPAY_MERCHANT_ID = "3002607"
ECPAY_HASH_KEY = "pwFHCqoQZGmho4w6"
ECPAY_HASH_IV = "EkRm7iFT261dpevs"

NEWEBPAY_MERCHANT_ID = "MS127874575"
NEWEBPAY_HASH_KEY = "Fs5cX1TGqYM2PpdbE14a9H83YQSQF5jn"
NEWEBPAY_HASH_IV = "C6AcmfqJILwgnhIP"

BASE_URL = "https://demo.example.test"


@pytest.fixture()
def app():
    app_instance = create_app(
        Settings(
            database_url="sqlite:///:memory:",
            base_url=BASE_URL,
            ecpay_merchant_id=ECPAY_MERCHANT_ID,
            ecpay_hash_key=ECPAY_HASH_KEY,
            ecpay_hash_iv=ECPAY_HASH_IV,
            newebpay_merchant_id=NEWEBPAY_MERCHANT_ID,
            newebpay_hash_key=NEWEBPAY_HASH_KEY,
            newebpay_hash_iv=NEWEBPAY_HASH_IV,
            _env_file=None,
        )
    )
    init_db(app_instance.state.engine)
    return app_instance


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def place_order(client: TestClient, gateway: str, method: str, product_id: int = 1) -> str:
    """建一筆訂單並回傳 order_no（金額 = 商品 1 的 450 元，除非另指定）。"""
    response = client.post(
        "/orders",
        data={"product_id": str(product_id), "gateway": gateway, "method": method},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]  # /orders/{order_no}/checkout
    return location.split("/")[2]


def order_status(client: TestClient, order_no: str) -> str:
    response = client.get(f"/api/orders/{order_no}/status")
    assert response.status_code == 200
    return response.json()["status"]
