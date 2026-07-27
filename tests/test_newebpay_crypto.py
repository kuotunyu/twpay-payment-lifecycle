"""藍新 TradeInfo AES-256-CBC／TradeSha：以現行官方手冊範例值驗證手寫實作。

範例出處：《線上交易-幕前支付技術串接手冊》NDNF-1.2.3 §4.1（docs/vendor/NDNF-1.2.3.pdf）。
"""
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from twpay_checkout.gateways.newebpay import (
    decrypt_trade_info,
    encrypt_trade_info,
    make_trade_sha,
    verify_trade_sha,
)

HASH_KEY = "Fs5cX1TGqYM2PpdbE14a9H83YQSQF5jn"
HASH_IV = "C6AcmfqJILwgnhIP"

# ---- 請求方向（手冊 §4.1.1–4.1.2）----
REQUEST_PARAMS = {
    "MerchantID": "MS127874575",
    "RespondType": "String",
    "TimeStamp": "1695795410",
    "Version": "2.0",
    "MerchantOrderNo": "Vanespl_ec_1695795410",
    "Amt": "30",
    "ItemDesc": "test",
    "NotifyURL": "https://webhook.site/d4db5ad1-2278-466a-9d66-78585c0dbadb",
}
OFFICIAL_REQUEST_CIPHER = (
    "f79eac33c4f3245d58f17b544c5d38b09457a6d77e77bae6f10fcc7236fe153ccef1a80"
    "001c0746afc063a7570f80ad970d8a32c72332c9ec5547410188007876bdca2bafa52d0"
    "7d31b6b183f2204d6e4feee6d245e286ab198cf95422ad5843c7696fc943cbb65979ad2"
    "07607d4b5d97dac4a90ccd5e7a37adb7d7062e838be09d94e8c5dfa145c048e17feabe5"
    "8c2e310792f0f50f5af32961ffb07ff6649ae1021ad558242551de5f09316e3182e1987"
    "75e5d1ad5b66a70be290004de750fa85d86b0c2f087b40005d89e048be2ab6fd83f1c52"
    "2494c093426a10a1f73fe4"
)
OFFICIAL_REQUEST_SHA = "84E4D9F96537E029F8450BE1E759080F9AF6995921B7F6F9AAFDDD2C36E7B287"

# ---- Notify 回傳方向（手冊 §4.1.3–4.1.4）----
OFFICIAL_NOTIFY_CIPHER = (
    "ee11d1501e6dc8433c75988258f2343d11f4d0a423be672e8e02aaf373c53c2363aeffdb"
    "4992579693277359b3e449ebe644d2075fdfbc10150b1c40e7d24cb215febefdb85b16a5"
    "cde449f6b06c58a5510d31e8d34c95284d459ae4b52afc1509c2800976a5c0b99ef24cfd"
    "28a2dfc8004215a0c98a1d3c77707773c2f2132f9a9a4ce3475cb888c2ad372485971876"
    "f8e2fec0589927544c3463d30c785c2d3bd947c06c8c33cf43e131f57939e1f7e3b3d8c3"
    "f08a84f34ef1a67a08efe177f1e663ecc6bedc7f82640a1ced807b548633cfa72d060864"
    "271ec79854ee2f5a170aa902000e7c61d1269165de330fce7d10663d1668c71157177636"
    "5bfdcd7ddc915dcb90d31a9f27af9b79a443ca8302e508b0dbaac817d44cfc44247ae613"
    "075dde4ac960f1bdff4173b915e4344bc4567bd32e86be7d796e6d9b9cf20476e4996e98"
    "ccc315f1ed03a34139f936797d971f2a3f90bc18f8a155a290bcbcf04f4277171c305bf5"
    "54f5cba243154b30082748a81f2e5aa432ef9950cc9668cd4330ef7c37537a6dcb5e6ef0"
    "1b4eca9705e4b097cf6913ee96e81d0389e5f775"
)
OFFICIAL_NOTIFY_SHA = "C80876AEBAC0036268C0E240E5BFF69C0470DE9606EEE083C5C8DD64FDB3347A"
OFFICIAL_NOTIFY_PLAINTEXT = (
    "Status=SUCCESS&Message=%E6%8E%88%E6%AC%8A%E6%88%90%E5%8A%9F&MerchantID="
    "MS127874575&Amt=30&TradeNo=23092714215835071&MerchantOrderNo=Vanespl_ec"
    "_1695795668&RespondType=String&IP=123.51.237.115&EscrowBank=HNCB&Paymen"
    "tType=CREDIT&RespondCode=00&Auth=115468&Card6No=400022&Card4No=1111&Exp"
    "=2609&AuthBank=KGI&TokenUseStatus=0&InstFirst=0&InstEach=0&Inst=0&ECI=&"
    "PayTime=2023-09-27+14%3A21%3A59&PaymentMethod=CREDIT"
)


def test_encrypt_matches_official_example():
    assert encrypt_trade_info(REQUEST_PARAMS, HASH_KEY, HASH_IV) == OFFICIAL_REQUEST_CIPHER


def test_trade_sha_matches_official_example():
    assert make_trade_sha(OFFICIAL_REQUEST_CIPHER, HASH_KEY, HASH_IV) == OFFICIAL_REQUEST_SHA


def test_decrypt_roundtrip_of_request():
    plaintext = decrypt_trade_info(OFFICIAL_REQUEST_CIPHER, HASH_KEY, HASH_IV)
    assert "MerchantOrderNo=Vanespl_ec_1695795410" in plaintext


def test_notify_trade_sha_verifies():
    assert verify_trade_sha(OFFICIAL_NOTIFY_CIPHER, OFFICIAL_NOTIFY_SHA, HASH_KEY, HASH_IV)
    # 大小寫不敏感（收到小寫也要能過）
    assert verify_trade_sha(
        OFFICIAL_NOTIFY_CIPHER, OFFICIAL_NOTIFY_SHA.lower(), HASH_KEY, HASH_IV
    )


def test_notify_decrypts_to_official_plaintext():
    assert (
        decrypt_trade_info(OFFICIAL_NOTIFY_CIPHER, HASH_KEY, HASH_IV)
        == OFFICIAL_NOTIFY_PLAINTEXT
    )


def test_verify_rejects_tampered_cipher_or_sha():
    tampered = "00" + OFFICIAL_NOTIFY_CIPHER[2:]
    assert not verify_trade_sha(tampered, OFFICIAL_NOTIFY_SHA, HASH_KEY, HASH_IV)
    assert not verify_trade_sha(OFFICIAL_NOTIFY_CIPHER, "A" * 64, HASH_KEY, HASH_IV)
    assert not verify_trade_sha(OFFICIAL_NOTIFY_CIPHER, "", HASH_KEY, HASH_IV)


def test_decrypt_tolerates_blocksize_32_padding():
    """藍新官方實作以 blocksize=32 填充——padding 值可能落在 17–32，
    嚴格的 16-byte PKCS7 unpadder 會解不開，我們的實作必須容忍。"""
    plaintext = b"Status=SUCCESS&Amt=30"  # 21 bytes → pad 到 32 需 11；改用 pad 值 27 模擬
    pad_len = 32 - len(plaintext) % 32  # = 11... 構造 >16 的情境：補到 48
    pad_len = 48 - len(plaintext)  # 27 bytes padding（>16）
    padded = plaintext + bytes([pad_len]) * pad_len
    encryptor = Cipher(
        algorithms.AES(HASH_KEY.encode()), modes.CBC(HASH_IV.encode())
    ).encryptor()
    cipher_hex = (encryptor.update(padded) + encryptor.finalize()).hex()

    assert decrypt_trade_info(cipher_hex, HASH_KEY, HASH_IV) == plaintext.decode()


def test_decrypt_rejects_garbage_padding():
    plaintext = b"x" * 15 + b"\x00"  # 最後一個 byte 0 → 非法 padding 值
    encryptor = Cipher(
        algorithms.AES(HASH_KEY.encode()), modes.CBC(HASH_IV.encode())
    ).encryptor()
    cipher_hex = (encryptor.update(plaintext) + encryptor.finalize()).hex()
    with pytest.raises(ValueError):
        decrypt_trade_info(cipher_hex, HASH_KEY, HASH_IV)
