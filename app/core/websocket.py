"""
云英AI WebSocket管理器
- 实时双向对话（流式回复）
- 健康数据实时流
- 主动关怀消息推送
"""
import json
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import WebSocket
from loguru import logger


class ConnectionManager:
    """WebSocket连接管理器，按用户隔离"""

    def __init__(self):
        # user_id -> WebSocket
        self._connections: dict[str, WebSocket] = {}
        # user_id -> 锁（防止并发写入）
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def connect(self, websocket: WebSocket, user_id: str):
        """接受WebSocket连接"""
        await websocket.accept()
        # 如果已有连接，关闭旧的
        if user_id in self._connections:
            try:
                await self._connections[user_id].close()
            except Exception:
                pass
        self._connections[user_id] = websocket
        logger.info(f"WebSocket connected: {user_id}")

    def disconnect(self, user_id: str):
        """断开WebSocket连接"""
        self._connections.pop(user_id, None)
        self._locks.pop(user_id, None)
        logger.info(f"WebSocket disconnected: {user_id}")

    def is_connected(self, user_id: str) -> bool:
        """检查用户是否在线"""
        return user_id in self._connections

    async def send_to_user(self, user_id: str, data: dict) -> bool:
        """向指定用户发送消息"""
        if user_id not in self._connections:
            return False
        lock = self._get_lock(user_id)
        async with lock:
            try:
                ws = self._connections[user_id]
                await ws.send_json(data)
                return True
            except Exception as e:
                logger.warning(f"WebSocket send failed for {user_id}: {e}")
                self.disconnect(user_id)
                return False

    async def send_care_message(
        self,
        user_id: str,
        message: str,
        care_type: str = "health_alert",
        blocks: list | None = None,
        severity: str = "info",
    ) -> bool:
        """
        发送主动关怀消息

        Args:
            user_id: 用户ID
            message: 关怀消息内容
            care_type: 关怀类型 health_alert/emotional_care/meditation_reminder/seasonal_tip
            blocks: 结构化内容块
            severity: 严重程度 info/warning/urgent
        """
        payload = {
            "type": "care_message",
            "care_type": care_type,
            "severity": severity,
            "message": message,
            "blocks": blocks or [],
            "timestamp": datetime.now().isoformat(),
        }
        return await self.send_to_user(user_id, payload)

    async def send_chat_chunk(self, user_id: str, chunk: str, done: bool = False) -> bool:
        """
        发送流式对话块

        Args:
            user_id: 用户ID
            chunk: 文本块
            done: 是否是最后一个块
        """
        payload = {
            "type": "chat_chunk",
            "content": chunk,
            "done": done,
            "timestamp": datetime.now().isoformat(),
        }
        return await self.send_to_user(user_id, payload)

    async def send_chat_complete(
        self,
        user_id: str,
        reply: str,
        engine: str,
        intent: str,
        blocks: list | None = None,
    ) -> bool:
        """
        发送完整对话回复

        Args:
            user_id: 用户ID
            reply: 完整回复
            engine: 引擎名
            intent: 意图
            blocks: 结构化内容块
        """
        payload = {
            "type": "chat_complete",
            "reply": reply,
            "engine": engine,
            "intent": intent,
            "blocks": blocks or [],
            "timestamp": datetime.now().isoformat(),
        }
        return await self.send_to_user(user_id, payload)

    async def send_health_update(self, user_id: str, event: dict) -> bool:
        """
        发送健康事件通知

        Args:
            user_id: 用户ID
            event: 健康事件详情
        """
        payload = {
            "type": "health_event",
            "event": event,
            "timestamp": datetime.now().isoformat(),
        }
        return await self.send_to_user(user_id, payload)

    @property
    def online_count(self) -> int:
        """当前在线用户数"""
        return len(self._connections)

    def get_online_users(self) -> list[str]:
        """获取所有在线用户ID"""
        return list(self._connections.keys())


# 全局单例
ws_manager = ConnectionManager()
