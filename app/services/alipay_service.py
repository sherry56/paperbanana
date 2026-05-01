from __future__ import annotations

import base64
import json
from datetime import datetime
from urllib.parse import quote_plus

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.app_config import load_app_config


APP_CONFIG = load_app_config()


def _normalize_pem(key: str, private: bool) -> bytes:
    raw = (key or "").strip()
    if not raw:
        return b""
    if "BEGIN" in raw:
        return raw.encode("utf-8")
    line = 64
    chunks = [raw[i : i + line] for i in range(0, len(raw), line)]
    body = "\n".join(chunks)
    if private:
        return f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n".encode("utf-8")
    return f"-----BEGIN PUBLIC KEY-----\n{body}\n-----END PUBLIC KEY-----\n".encode("utf-8")


def _build_sign_content(params: dict[str, str]) -> str:
    items = [(k, v) for k, v in params.items() if v is not None and v != "" and k not in {"sign", "sign_type"}]
    items.sort(key=lambda x: x[0])
    return "&".join([f"{k}={v}" for k, v in items])


def _sign_rsa2(content: str, private_key: str) -> str:
    private_pem = _normalize_pem(private_key, private=True)
    key = serialization.load_pem_private_key(private_pem, password=None)
    signature = key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")


def verify_notify_rsa2(params: dict[str, str], sign: str) -> bool:
    public_pem = _normalize_pem(APP_CONFIG.alipay_public_key, private=False)
    if not public_pem or not sign:
        return False
    content = _build_sign_content(params)
    pub = serialization.load_pem_public_key(public_pem)
    try:
        pub.verify(base64.b64decode(sign), content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def create_page_pay_url(
    *,
    out_trade_no: str,
    subject: str,
    total_amount_yuan: float,
) -> str:
    biz = {
        "out_trade_no": out_trade_no,
        "product_code": "FAST_INSTANT_TRADE_PAY",
        "total_amount": f"{float(total_amount_yuan):.2f}",
        "subject": subject[:128],
    }
    if APP_CONFIG.alipay_seller_id:
        biz["seller_id"] = APP_CONFIG.alipay_seller_id
    params: dict[str, str] = {
        "app_id": APP_CONFIG.alipay_app_id,
        "method": "alipay.trade.page.pay",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": APP_CONFIG.alipay_notify_url,
        "return_url": APP_CONFIG.alipay_return_url,
        "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
    }
    sign_content = _build_sign_content(params)
    sign = _sign_rsa2(sign_content, APP_CONFIG.alipay_app_private_key)
    params["sign"] = sign
    query = "&".join([f"{k}={quote_plus(str(v))}" for k, v in params.items() if v is not None and v != ""])
    return f"{APP_CONFIG.alipay_gateway}?{query}"


def alipay_config_ready() -> bool:
    return bool(
        APP_CONFIG.alipay_enabled
        and APP_CONFIG.alipay_app_id
        and APP_CONFIG.alipay_notify_url
        and APP_CONFIG.alipay_app_private_key
        and APP_CONFIG.alipay_public_key
    )
