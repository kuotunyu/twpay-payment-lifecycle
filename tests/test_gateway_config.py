"""Gateway constructors must reject unsafe sandbox credential settings."""

import pytest

from twpay_checkout.config import Settings
from twpay_checkout.gateways.ecpay import EcpayGateway
from twpay_checkout.gateways.newebpay import NewebpayGateway


def settings(**overrides: str) -> Settings:
    values = {
        "ecpay_merchant_id": "3002607",
        "ecpay_hash_key": "pwFHCqoQZGmho4w6",
        "ecpay_hash_iv": "EkRm7iFT261dpevs",
        "newebpay_merchant_id": "MS127874575",
        "newebpay_hash_key": "Fs5cX1TGqYM2PpdbE14a9H83YQSQF5jn",
        "newebpay_hash_iv": "C6AcmfqJILwgnhIP",
    }
    values.update(overrides)
    return Settings(**values, _env_file=None)


@pytest.mark.parametrize(
    ("gateway_cls", "overrides", "missing_name"),
    [
        (EcpayGateway, {"ecpay_hash_key": ""}, "HashKey"),
        (NewebpayGateway, {"newebpay_merchant_id": "  "}, "MerchantID"),
    ],
)
def test_gateway_rejects_missing_credentials(gateway_cls, overrides, missing_name):
    with pytest.raises(ValueError, match=missing_name):
        gateway_cls(settings(**overrides))


@pytest.mark.parametrize(
    ("gateway_cls", "overrides", "expected_length"),
    [
        (EcpayGateway, {"ecpay_hash_iv": "too-short"}, "16"),
        (NewebpayGateway, {"newebpay_hash_key": "too-short"}, "32"),
    ],
)
def test_gateway_rejects_invalid_key_lengths(
    gateway_cls, overrides, expected_length
):
    with pytest.raises(ValueError, match=expected_length):
        gateway_cls(settings(**overrides))


def test_gateways_accept_documented_sandbox_credential_shapes():
    configured = settings()
    assert EcpayGateway(configured).merchant_id == "3002607"
    assert NewebpayGateway(configured).merchant_id == "MS127874575"
