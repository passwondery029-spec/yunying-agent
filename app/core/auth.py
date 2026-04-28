"""JWT认证鉴权系统"""

import os
import time
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Header, HTTPException
from passlib.context import CryptContext
from pydantic import BaseModel, Field


# === 数据模型 ===

class TokenData(BaseModel):
    """JWT token 解析后的用户数据"""
    user_id: str
    username: str
    token_type: str = "access"


# === 配置 ===

JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_hex(32))  # 每次启动随机生成，生产环境必须固定
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "72"))  # token有效期72小时
JWT_REFRESH_DAYS = int(os.getenv("JWT_REFRESH_DAYS", "30"))  # 刷新token有效期30天

# 密码加密
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# === 数据模型 ===

class UserCreate(BaseModel):
    """用户注册请求"""
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.@\-_\u4e00-\u9fff]+$")
    password: str = Field(min_length=6, max_length=64)
    nickname: str | None = Field(default=None, max_length=32)


class UserLogin(BaseModel):
    """用户登录请求"""
    username: str
    password: str


class TokenResponse(BaseModel):
    """Token响应"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
    user_id: str
    username: str


class TokenPayload(BaseModel):
    """Token载荷"""
    user_id: str
    username: str
    exp: float
    iat: float
    type: str  # "access" | "refresh"


# === 密码工具 ===

def hash_password(password: str) -> str:
    """哈希密码"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return pwd_context.verify(plain_password, hashed_password)


# === Token工具 ===

def create_access_token(user_id: str, username: str) -> tuple[str, int]:
    """创建访问token，返回(token, 过期时间戳)"""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {
        "user_id": user_id,
        "username": username,
        "type": "access",
        "iat": now.timestamp(),
        "exp": expire.timestamp(),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, int(expire.timestamp())


def create_refresh_token(user_id: str, username: str) -> str:
    """创建刷新token"""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=JWT_REFRESH_DAYS)
    payload = {
        "user_id": user_id,
        "username": username,
        "type": "refresh",
        "iat": now.timestamp(),
        "exp": expire.timestamp(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[TokenPayload]:
    """解码并验证token，失败返回None"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return TokenPayload(
            user_id=payload["user_id"],
            username=payload["username"],
            exp=payload["exp"],
            iat=payload["iat"],
            type=payload["type"],
        )
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, KeyError):
        return None


def create_token_pair(user_id: str, username: str) -> TokenResponse:
    """创建token对（访问+刷新）"""
    access_token, expires_at = create_access_token(user_id, username)
    refresh_token = create_refresh_token(user_id, username)
    now = time.time()
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_at - int(now),
        user_id=user_id,
        username=username,
    )


# === 用户存储（SQLite持久化） ===

class UserStore:
    """用户数据存储，使用SQLite"""

    def __init__(self, db_path: str = "data/users.db"):
        self.db_path = db_path
        self._initialized = False

    async def _ensure_init(self):
        """确保数据库表已创建"""
        if self._initialized:
            return
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    nickname TEXT,
                    created_at REAL NOT NULL,
                    last_login REAL,
                    is_active INTEGER DEFAULT 1
                )
            """)
            await db.commit()
        self._initialized = True

    async def create_user(self, username: str, password: str, nickname: str | None = None) -> dict | None:
        """创建用户，成功返回用户信息，用户名重复返回None"""
        await self._ensure_init()
        import aiosqlite
        user_id = f"u_{secrets.token_hex(8)}"
        password_hash = hash_password(password)
        now = time.time()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO users (user_id, username, password_hash, nickname, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, username, password_hash, nickname, now),
                )
                await db.commit()
            return {"user_id": user_id, "username": username, "nickname": nickname}
        except aiosqlite.IntegrityError:
            return None  # 用户名已存在

    async def verify_user(self, username: str, password: str) -> dict | None:
        """验证用户登录，成功返回用户信息"""
        await self._ensure_init()
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT user_id, username, nickname, password_hash FROM users WHERE username = ? AND is_active = 1",
                (username,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            if not verify_password(password, row["password_hash"]):
                return None
            # 更新最后登录时间
            await db.execute("UPDATE users SET last_login = ? WHERE user_id = ?", (time.time(), row["user_id"]))
            await db.commit()
            return {"user_id": row["user_id"], "username": row["username"], "nickname": row["nickname"]}

    async def get_user(self, user_id: str) -> dict | None:
        """获取用户信息"""
        await self._ensure_init()
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT user_id, username, nickname, created_at, last_login FROM users WHERE user_id = ? AND is_active = 1",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)


# 全局单例
user_store = UserStore()


# === 认证中间件 ===

# 不需要认证的路径
PUBLIC_PATHS = {
    "/health",
    "/docs",
    "/openapi.json",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/",
    "/index.html",
}


def is_public_path(path: str) -> bool:
    """判断是否为公开路径"""
    if path in PUBLIC_PATHS:
        return True
    # 静态文件
    if path.startswith("/static/") or path.startswith("/favicon"):
        return True
    return False


async def get_current_user(authorization: str | None) -> dict | None:
    """从Authorization头解析当前用户"""
    if not authorization:
        return None
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    payload = decode_token(token)
    if payload is None:
        return None
    if payload.type != "access":
        return None
    # 从数据库验证用户仍存在且活跃
    user = await user_store.get_user(payload.user_id)
    return user


def require_auth(authorization: str = Header(None)) -> TokenData:
    """FastAPI 依赖：要求认证，未认证返回401"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="未登录或token已过期，请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:]
    payload = decode_token(token)
    if not payload or payload.type != "access":
        raise HTTPException(
            status_code=401,
            detail="未登录或token已过期，请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload
