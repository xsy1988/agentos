"""auth 路由：初始化引导 / 登录 / 当前用户。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.modules.auth.deps import get_current_user
from app.modules.auth.models import User
from app.modules.auth.schemas import TokenOut, UserInitIn, UserLoginIn, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/init", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def init_user(body: UserInitIn, db: AsyncSession = Depends(get_db)) -> User:
    """单用户初始化引导：仅在库中无账号时可用。"""
    count = await db.scalar(select(func.count()).select_from(User))
    if count:
        raise HTTPException(status.HTTP_409_CONFLICT, "账号已初始化，请直接登录")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenOut)
async def login(body: UserLoginIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    user = await db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    return TokenOut(access_token=create_access_token(str(user.id)))


@router.get("/me", response_model=UserOut)
async def read_me(user: User = Depends(get_current_user)) -> User:
    return user
