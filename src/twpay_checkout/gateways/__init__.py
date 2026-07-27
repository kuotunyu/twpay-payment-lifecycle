"""Gateway adapter registry."""
from ..config import Settings
from ..models import GatewayName
from .base import PaymentGateway


def get_gateway(name: GatewayName, settings: Settings) -> PaymentGateway:
    if name == GatewayName.ECPAY:
        from .ecpay import EcpayGateway

        return EcpayGateway(settings)
    if name == GatewayName.NEWEBPAY:
        from .newebpay import NewebpayGateway

        return NewebpayGateway(settings)
    raise ValueError(f"Unknown gateway: {name}")
