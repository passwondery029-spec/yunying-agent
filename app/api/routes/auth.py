"""认证API路由"""

from fastapi import APIRouter, HTTPException, Request, Depends
from loguru import logger
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


@router.post("/guest", response_model=TokenResponse)
async def guest_login():
    """访客自动登录：无需注册，自动创建访客账号"""
    try:
        import time as _time
        guest_id = f"guest_{secrets.token_hex(4)}"
        # 用时间戳生成唯一用户名
        username = f"访客{_time.time_ns() % 1000000}"
        user = await user_store.create_user(
            username=username,
            password=secrets.token_hex(16),  # 随机密码，访客不需要知道
            nickname="访客",
        )
        if user is None:
            # 极小概率冲突，重试
            username = f"访客{_time.time_ns() % 10000000}"
            user = await user_store.create_user(
                username=username,
                password=secrets.token_hex(16),
                nickname="访客",
            )
        if user is None:
            raise HTTPException(status_code=500, detail="创建访客账号失败")
        return create_token_pair(user["user_id"], user["username"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"访客登录失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="访客登录失败，请稍后重试")


@router.post("/register", response_model=TokenResponse)
async def register(body: UserCreate):
    """用户注册"""
    try:
        user = await user_store.create_user(
            username=body.username,
            password=body.password,
            nickname=body.nickname or body.username,
        )
        if user is None:
            raise HTTPException(status_code=409, detail="用户名已存在")
        return create_token_pair(user["user_id"], user["username"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"注册失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"注册失败，请稍后重试")


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin):
    """用户登录"""
    try:
        user = await user_store.verify_user(body.username, body.password)
        if user is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        return create_token_pair(user["user_id"], user["username"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"登录失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"登录失败，请稍后重试")


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
