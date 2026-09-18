"""security 纯逻辑单测：密码哈希 + JWT 往返。"""

import pytest

from app.core.config import settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

# PyJWT 对 <32 字节的 HMAC 密钥发 InsecureKeyLengthWarning；测试只用密钥做本地往返，
# 因此换成够长的测试密钥，生产缺省（settings.secret_key）保持不变
_TEST_SECRET_KEY = "test-secret-key-32-bytes-minimum-0001"


@pytest.fixture(autouse=True)
def _strong_test_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", _TEST_SECRET_KEY)


def test_password_roundtrip() -> None:
    h = hash_password("s3cret-pass")
    assert h != "s3cret-pass"
    assert verify_password("s3cret-pass", h)
    assert not verify_password("wrong-pass", h)


def test_password_hash_salt() -> None:
    # 同密码两次哈希应不同（加盐）
    assert hash_password("same-pass") != hash_password("same-pass")


def test_jwt_roundtrip() -> None:
    token = create_access_token("11111111-1111-1111-1111-111111111111")
    assert decode_access_token(token) == "11111111-1111-1111-1111-111111111111"


def test_jwt_invalid() -> None:
    assert decode_access_token("not-a-token") is None
    assert decode_access_token("") is None
