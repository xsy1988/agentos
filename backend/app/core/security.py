"""密码哈希与 JWT 签发校验。

不用 passlib：其已停止维护且与 bcrypt>=4 存在兼容问题，直接使用 bcrypt。
"""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import settings

_ALGO = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: str) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.secret_key, algorithm=_ALGO)


def decode_access_token(token: str) -> str | None:
    """返回 user_id；无效或过期返回 None。"""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[_ALGO])
    except jwt.InvalidTokenError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None
