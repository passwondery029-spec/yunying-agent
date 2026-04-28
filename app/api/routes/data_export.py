"""数据导出API — 供运营分析用"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from loguru import logger
import aiosqlite
import json
import os

from app.core.auth import require_auth, TokenData
from app.core.database import DB_PATH

router = APIRouter(prefix="/data", tags=["data"])

# 简单的管理员密钥验证（避免任何人都能导出）
import os as _os
_ADMIN_KEY = _os.getenv("ADMIN_KEY", "")


def _check_admin(admin_key: str = Query(None)):
    """验证管理员密钥"""
    if not _ADMIN_KEY:
        raise ValueError("未配置 ADMIN_KEY 环境变量，无法使用导出功能")
    if admin_key != _ADMIN_KEY:
        raise ValueError("管理员密钥错误")


@router.get("/messages/export")
async def export_messages(
    admin_key: str = Query(..., description="管理员密钥"),
    limit: int = Query(5000, description="最多导出条数", ge=1, le=50000),
    since: str | None = Query(None, description="起始时间，如 2026-03-01"),
):
    """
    导出聊天记录（JSON格式）
    
    用法: GET /api/v1/data/messages/export?admin_key=xxx&since=2026-03-01&limit=5000
    
    返回字段：
    - id, session_id, user_id, role, content, engine, intent, created_at
    """
    _check_admin(admin_key)
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        query = "SELECT id, session_id, user_id, role, content, engine, intent, created_at FROM messages"
        params = []
        
        if since:
            query += " WHERE created_at >= ?"
            params.append(since)
        
        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        
        messages = [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "user_id": r["user_id"],
                "role": r["role"],
                "content": r["content"],
                "engine": r["engine"],
                "intent": r["intent"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    
    logger.info(f"导出聊天记录: {len(messages)} 条, since={since}")
    return JSONResponse(content={"count": len(messages), "messages": messages})


@router.get("/stats")
async def get_stats(
    admin_key: str = Query(..., description="管理员密钥"),
):
    """
    数据概览：用户数、消息数、会话数等
    
    用法: GET /api/v1/data/stats?admin_key=xxx
    """
    _check_admin(admin_key)
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        stats = {}
        
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM messages")
        stats["total_messages"] = (await cursor.fetchone())["cnt"]
        
        cursor = await db.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM messages")
        stats["total_users"] = (await cursor.fetchone())["cnt"]
        
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM sessions")
        stats["total_sessions"] = (await cursor.fetchone())["cnt"]
        
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM health_metrics")
        stats["total_health_records"] = (await cursor.fetchone())["cnt"]
        
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM memory_fragments WHERE is_valid = 1")
        stats["total_memory_fragments"] = (await cursor.fetchone())["cnt"]
        
        # 最近7天消息数
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE created_at >= datetime('now', '-7 days')"
        )
        stats["messages_last_7d"] = (await cursor.fetchone())["cnt"]
        
        # 意图分布
        cursor = await db.execute(
            "SELECT intent, COUNT(*) as cnt FROM messages WHERE intent != '' GROUP BY intent ORDER BY cnt DESC LIMIT 10"
        )
        stats["intent_distribution"] = [
            {"intent": r["intent"], "count": r["cnt"]} for r in await cursor.fetchall()
        ]
        
        # 引擎分布
        cursor = await db.execute(
            "SELECT engine, COUNT(*) as cnt FROM messages WHERE engine != '' AND role = 'assistant' GROUP BY engine ORDER BY cnt DESC"
        )
        stats["engine_distribution"] = [
            {"engine": r["engine"], "count": r["cnt"]} for r in await cursor.fetchall()
        ]
    
    return JSONResponse(content=stats)
