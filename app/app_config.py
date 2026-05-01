from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class AppConfig:
    # 本地默认配置（敏感值请通过环境变量覆盖）
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "paperfigure"
    # 本地开发默认，建议用环境变量覆盖
    db_password: str = "802355"
    db_name: str = "paperfigure"
    db_charset: str = "utf8mb4"

    # 管理员初始化配置（建议生产通过环境变量注入）
    admin_username: str = "B308"
    # 本地开发默认，建议用环境变量覆盖
    admin_password: str = "802355"

    # 支付宝配置（默认关闭；生产环境通过 PB_ALIPAY_* 注入）
    alipay_enabled: bool = False
    alipay_app_id: str = ""
    # 生产环境: https://openapi.alipay.com/gateway.do
    # 沙箱环境: https://openapi-sandbox.dl.alipaydev.com/gateway.do
    alipay_gateway: str = "https://openapi.alipay.com/gateway.do"
    alipay_notify_url: str = ""
    alipay_return_url: str = ""
    # 私钥/公钥必须通过环境变量注入，避免硬编码泄露
    alipay_app_private_key: str = ""
    alipay_public_key: str = ""
    alipay_seller_id: str = ""
    # 可选：收款码图片 URL（建议放在 /assets 下）
    alipay_qr_image_url: str = ""
    wechat_qr_image_url: str = ""

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset={self.db_charset}"
        )


def _env_bool(name: str, default: bool) -> bool:
    """未设置环境变量时使用 default；显式 0/false/off 为关闭。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_app_config() -> AppConfig:
    """
    默认全部走代码内配置；如需临时覆盖，环境变量仍可用（非必需）。
    """
    base = AppConfig()
    return AppConfig(
        db_host=os.getenv("PB_DB_HOST", base.db_host),
        db_port=int(os.getenv("PB_DB_PORT", str(base.db_port))),
        db_user=os.getenv("PB_DB_USER", base.db_user),
        db_password=os.getenv("PB_DB_PASSWORD", base.db_password),
        db_name=os.getenv("PB_DB_NAME", base.db_name),
        db_charset=os.getenv("PB_DB_CHARSET", base.db_charset),
        admin_username=os.getenv("PB_ADMIN_USERNAME", base.admin_username),
        admin_password=os.getenv("PB_ADMIN_PASSWORD", base.admin_password),
        alipay_enabled=_env_bool("PB_ALIPAY_ENABLED", base.alipay_enabled),
        alipay_app_id=os.getenv("PB_ALIPAY_APP_ID", base.alipay_app_id),
        alipay_gateway=os.getenv("PB_ALIPAY_GATEWAY", base.alipay_gateway),
        alipay_notify_url=os.getenv("PB_ALIPAY_NOTIFY_URL", base.alipay_notify_url),
        alipay_return_url=os.getenv("PB_ALIPAY_RETURN_URL", base.alipay_return_url),
        alipay_app_private_key=os.getenv("PB_ALIPAY_APP_PRIVATE_KEY", base.alipay_app_private_key),
        alipay_public_key=os.getenv("PB_ALIPAY_PUBLIC_KEY", base.alipay_public_key),
        alipay_seller_id=os.getenv("PB_ALIPAY_SELLER_ID", base.alipay_seller_id),
        alipay_qr_image_url=os.getenv("PB_ALIPAY_QR_IMAGE_URL", base.alipay_qr_image_url),
        wechat_qr_image_url=os.getenv("PB_WECHAT_QR_IMAGE_URL", base.wechat_qr_image_url),
    )

