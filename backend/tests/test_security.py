"""security 纯逻辑单测：密码哈希 + JWT 往返。"""

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


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
