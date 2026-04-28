"""云英 AI Agent — FastAPI 入口"""

import time
import uuid
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from loguru import logger

from app.api.routes import chat, health, event, profile, memory, auth, health_manual
from app.api.schemas import HealthCheckResponse
from app.config import get_settings
from app.core.database import init_db

# === 初始化结构化日志 ===
import sys
import os

LOG_DIR = os.getenv("LOG_DIR", "data/logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 移除默认handler
logger.remove()
# 控制台：简洁格式
logger.add(
    sys.stderr,
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> | {message}",
)
# 文件：JSON结构化日志
logger.add(
    os.path.join(LOG_DIR, "yunying_{time:YYYY-MM-DD}.log"),
    level="DEBUG",
    format="{message}",
    serialize=True,  # JSON格式
    rotation="50 MB",
    retention="30 days",
    compression="gz",
    # 安全：过滤掉可能包含密钥的字段
    filter=lambda record: "api_key" not in str(record.get("extra", {})).lower(),
)
logger.info("云英AI日志系统启动")

settings = get_settings()

app = FastAPI(
    title="云英 AI",
    description="身心健康陪伴 Agent",
    version="0.2.0",
)

# CORS — 生产环境应限制 allow_origins
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS.split(",") if CORS_ORIGINS != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# === 请求追踪中间件 ===

@app.middleware("http")
async def request_trace_middleware(request: Request, call_next):
    """为每个请求分配唯一追踪ID，记录请求耗时"""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    request.state.request_id = request_id
    start_time = time.time()

    # 记录请求开始
    logger.info(
        f"请求开始: {request.method} {request.url.path}",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "client_ip": request.client.host if request.client else None,
        },
    )

    try:
        response = await call_next(request)
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            f"请求异常: {request.method} {request.url.path} - {e}",
            extra={
                "request_id": request_id,
                "elapsed_ms": int(elapsed * 1000),
                "error": str(e),
            },
        )
        raise

    elapsed = time.time() - start_time
    # 记录请求完成
    logger.info(
        f"请求完成: {request.method} {request.url.path} [{response.status_code}] {int(elapsed*1000)}ms",
        extra={
            "request_id": request_id,
            "status_code": response.status_code,
            "elapsed_ms": int(elapsed * 1000),
        },
    )
    # 将追踪ID写入响应头
    response.headers["X-Request-ID"] = request_id
    return response


# 注册路由
app.include_router(auth.router)       # 认证路由（无前缀，内部自带 /api/v1/auth）
# app.include_router(ws.router)       # WebSocket路由（暂未就绪）
app.include_router(chat.router, prefix="/api/v1")
app.include_router(health.router, prefix="/api/v1")
app.include_router(event.router, prefix="/api/v1")
app.include_router(profile.router, prefix="/api/v1")
app.include_router(memory.router, prefix="/api/v1")
app.include_router(health_manual.router, prefix="/api/v1")


@app.on_event("startup")
async def startup():
    """应用启动时初始化数据库"""
    from app.core.database import init_db
    await init_db()
    logger.info("数据库初始化完成")


@app.get("/health", response_model=HealthCheckResponse)
async def health_check():
    """健康检查"""
    return HealthCheckResponse()


@app.get("/")
async def index():
    """聊天界面"""
    return FileResponse("static/index.html")


# 静态文件
app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
