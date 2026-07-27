"""藍新 NewebPay MPG 幕前支付 adapter，TradeInfo AES-256-CBC／TradeSha 手寫實作。

規格來源：《線上交易-幕前支付技術串接手冊》NDNF-1.2.3（本地 docs/vendor/NDNF-1.2.3.pdf，
單元測試 fixture 即手冊 §4.1 官方範例值；詳 docs/gateway-notes.md）。
"""
import hashlib
import hmac
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Response
from fastapi.responses import PlainTextResponse

from ..config import Settings
from ..models import GatewayName, NotificationKind, Order, PaymentMethod
from .base import (
    AtmInfo,
    CallbackUrls,
    ParsedNotification,
    PaymentRedirect,
    RecurringPlan,
    SignatureError,
)

# 紅線：只允許測試環境端點（ccore），正式環境（core）一律禁止。
STAGE_MPG_URL = "https://ccore.newebpay.com/MPG/mpg_gateway"
STAGE_QUERY_URL = "https://ccore.newebpay.com/API/QueryTradeInfo"
_ALLOWED_TEST_HOST = "ccore.newebpay.com"

MPG_VERSION = "2.0"  # 手冊規格值為 2.3；2.0 完全相容且為官方範例採用（gateway-notes.md）
ATM_EXPIRE_DAYS = 7  # ExpireDate 官方預設：取號起算第 7 天 23:59:59

_AES_BLOCK = 16


def _assert_stage(url: str) -> str:
    host = urlsplit(url).hostname or ""
    if host != _ALLOWED_TEST_HOST:
        raise AssertionError(f"Refusing non-stage NewebPay endpoint: {url}")
    return url


def encrypt_trade_info(params: Mapping[str, str], hash_key: str, hash_iv: str) -> str:
    """交易參數 → UTF-8 query string → AES-256-CBC（PKCS7 填充）→ hex 小寫。

    key/iv 直接使用商店 HashKey（32 字元）/HashIV（16 字元）的 ASCII bytes。
    """
    plaintext = urlencode(params).encode("utf-8")
    pad_len = _AES_BLOCK - len(plaintext) % _AES_BLOCK
    padded = plaintext + bytes([pad_len]) * pad_len
    encryptor = Cipher(
        algorithms.AES(hash_key.encode("ascii")), modes.CBC(hash_iv.encode("ascii"))
    ).encryptor()
    return (encryptor.update(padded) + encryptor.finalize()).hex()


def decrypt_trade_info(cipher_hex: str, hash_key: str, hash_iv: str) -> str:
    """解密回傳的 TradeInfo。

    藍新官方實作以 blocksize=32 做 PKCS7「樣式」填充（手冊 §4.1.4 的
    strippadding 依最後一個 byte 的值去除），因此這裡容忍 1–32 的 padding 值，
    不可用嚴格的 16-byte PKCS7 unpadder。
    """
    decryptor = Cipher(
        algorithms.AES(hash_key.encode("ascii")), modes.CBC(hash_iv.encode("ascii"))
    ).decryptor()
    padded = decryptor.update(bytes.fromhex(cipher_hex)) + decryptor.finalize()
    if not padded:
        raise ValueError("empty ciphertext")
    pad_len = padded[-1]
    if not 1 <= pad_len <= 32 or padded[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("bad PKCS7-style padding")
    return padded[:-pad_len].decode("utf-8")


def make_trade_sha(cipher_hex: str, hash_key: str, hash_iv: str) -> str:
    """TradeSha = UPPER(SHA256("HashKey={key}&{密文}&HashIV={iv}"))。"""
    source = f"HashKey={hash_key}&{cipher_hex}&HashIV={hash_iv}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest().upper()


def verify_trade_sha(
    cipher_hex: str, trade_sha: str, hash_key: str, hash_iv: str
) -> bool:
    if not trade_sha:
        return False
    expected = make_trade_sha(cipher_hex, hash_key, hash_iv)
    return hmac.compare_digest(expected, trade_sha.upper())


def _sanitize_item_desc(name: str) -> str:
    """ItemDesc 限 50 字元、避免特殊符號（官方會自動過濾，先自清以防截斷）。"""
    return re.sub(r"[^0-9A-Za-z一-鿿\s-]", "", name).strip()[:50] or "item"


def _parse_deadline(date_text: str, time_text: str) -> datetime | None:
    """取號結果的 ExpireDate（YYYY-MM-DD 或 Ymd）＋ ExpireTime（HH:MM:SS 或 His）。"""
    day = None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            day = datetime.strptime(date_text, fmt)
            break
        except ValueError:
            continue
    if day is None:
        return None
    for fmt in ("%H:%M:%S", "%H%M%S"):
        try:
            clock = datetime.strptime(time_text, fmt)
            return day.replace(hour=clock.hour, minute=clock.minute, second=clock.second)
        except ValueError:
            continue
    return day.replace(hour=23, minute=59, second=59)


class NewebpayGateway:
    name = GatewayName.NEWEBPAY

    def __init__(self, settings: Settings):
        self.merchant_id = settings.newebpay_merchant_id
        self.hash_key = settings.newebpay_hash_key
        self.hash_iv = settings.newebpay_hash_iv
        self.mpg_url = _assert_stage(STAGE_MPG_URL)
        if not (self.merchant_id and self.hash_key and self.hash_iv):
            raise ValueError(
                "藍新測試商店金鑰未設定：請至 cwww.newebpay.com 申請並填入 .env"
            )

    def create_payment(
        self,
        order: Order,
        product_name: str,
        urls: CallbackUrls,
        recurring: RecurringPlan | None = None,
    ) -> PaymentRedirect:
        if recurring is not None:
            raise NotImplementedError("定期定額展示僅使用 ECPay 官方 sandbox")
        trade_params: dict[str, str] = {
            "MerchantID": self.merchant_id,
            "RespondType": "String",
            "TimeStamp": str(int(time.time())),  # 與藍新伺服器容許時差 ±120 秒
            "Version": MPG_VERSION,
            "MerchantOrderNo": order.order_no,
            "Amt": str(order.amount),  # 金額以 DB 訂單為唯一真相
            "ItemDesc": _sanitize_item_desc(product_name),
            "NotifyURL": urls.notify,  # 背景通知：入帳唯一依據
            "ReturnURL": urls.front_return,  # 前景導回（Form POST），只做顯示
        }
        if order.payment_method == PaymentMethod.CREDIT_CARD:
            trade_params["CREDIT"] = "1"
        else:
            trade_params["VACC"] = "1"
            expire = datetime.now() + timedelta(days=ATM_EXPIRE_DAYS)
            trade_params["ExpireDate"] = expire.strftime("%Y%m%d")
            trade_params["CustomerURL"] = urls.customer  # 取號完成導回（無背景取號通知）

        cipher = encrypt_trade_info(trade_params, self.hash_key, self.hash_iv)
        fields = {
            "MerchantID": self.merchant_id,
            "TradeInfo": cipher,
            "TradeSha": make_trade_sha(cipher, self.hash_key, self.hash_iv),
            "Version": MPG_VERSION,
            # EncryptType 不帶 = AES/CBC/PKCS7（EncryptType=1 的 GCM 官方未載明格式）
        }
        return PaymentRedirect(action_url=self.mpg_url, fields=fields)

    def verify_and_parse_notification(
        self, payload: Mapping[str, str], kind: NotificationKind
    ) -> ParsedNotification:
        cipher = payload.get("TradeInfo", "")
        trade_sha = payload.get("TradeSha", "")
        if not cipher or not verify_trade_sha(cipher, trade_sha, self.hash_key, self.hash_iv):
            raise SignatureError("NewebPay TradeSha mismatch", payload)
        try:
            plaintext = decrypt_trade_info(cipher, self.hash_key, self.hash_iv)
        except (ValueError, UnicodeDecodeError) as exc:
            # 驗簽過了卻解不開＝金鑰或格式異常，同樣視為不可信
            raise SignatureError(f"NewebPay TradeInfo decrypt failed: {exc}", payload) from exc

        data = dict(parse_qsl(plaintext, keep_blank_values=True))
        success = data.get("Status", "") == "SUCCESS"
        amount_text = data.get("Amt", "")
        amount = int(amount_text) if amount_text.isdigit() else None
        atm: AtmInfo | None = None
        paid_at: datetime | None = None

        if kind == NotificationKind.ATM_ACCOUNT_ISSUED:
            if success:
                atm = AtmInfo(
                    bank_code=data.get("BankCode", ""),
                    virtual_account=data.get("CodeNo", ""),
                    pay_deadline=_parse_deadline(
                        data.get("ExpireDate", ""), data.get("ExpireTime", "")
                    ),
                )
        elif success:
            try:
                paid_at = datetime.strptime(data.get("PayTime", ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                paid_at = None

        return ParsedNotification(
            kind=kind,
            order_no=data.get("MerchantOrderNo", ""),
            gateway_trade_no=data.get("TradeNo", ""),
            amount=amount,
            success=success,
            message=data.get("Message", ""),
            paid_at=paid_at,
            atm=atm,
            raw=dict(payload) | {"_decrypted": plaintext},
        )

    def success_response(self) -> Response:
        # 官方（NDNF-1.2.3 常見問題）：只認 HTTP 200，body 不拘；Retry 3 次
        return PlainTextResponse("ok", status_code=200)

    def failure_response(self, reason: str = "") -> Response:
        return PlainTextResponse(reason or "error", status_code=400)
