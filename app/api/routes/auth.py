"""认证API路由"""

from fastapi import APIRouter, HTTPException, Request, Depends
from app.core.auth import (
    UserCreate, UserLogin, TokenResponse,
    user_store, create_token_pair, decode_token, create_access_token, create_refresh_token,
    require_auth, TokenData,
)

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


@router.get("/me")
async def get_me(user: TokenData = Depends(require_auth)):
    """验证当前token并返回用户信息"""
    user_info = await user_store.get_user(user.user_id)
    return {
        "user_id": user.user_id,
        "username": user.username,
        "nickname": (user_info or {}).get("nickname", user.username),
    }


@router.post("/register", response_model=TokenResponse)
async def register(body: UserCreate):
    """用户注册"""
    user = await user_store.create_user(
        username=body.username,
        password=body.password,
        nickname=body.nickname or body.username,
    )
    if user is None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    return create_token_pair(user["user_id"], user["username"])


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin):
    """用户登录"""
    user = await user_store.verify_user(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return create_token_pair(user["user_id"], user["username"])


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(request: Request):
    """刷新访问token"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少刷新token")
    refresh = auth[7:]
    payload = decode_token(refresh)
    if payload is None or payload.type != "refresh":
        raise HTTPException(status_code=401, detail="刷新token无效或已过期")
    # 验证用户仍存在
    user = await user_store.get_user(payload.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    return create_token_pair(user["user_id"], user["username"])
