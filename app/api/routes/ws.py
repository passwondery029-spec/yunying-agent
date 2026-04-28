"""
WebSocket 端点 + 主动关怀引擎

WebSocket 协议:
  客户端 → 服务端:
    {"type": "chat", "message": "...", "health_data": {...}}
    {"type": "ping"}

  服务端 → 客户端:
    {"type": "chat_complete", "reply": "...", "engine": "...", "intent": "...", "blocks": [...]}
    {"type": "chat_chunk", "content": "...", "done": false}
    {"type": "care_message", "care_type": "...", "message": "...", "blocks": [...], "severity": "..."}
    {"type": "health_event", "event": {...}}
    {"type": "pong"}
    {"type": "error", "message": "..."}
"""
import asyncio
import json
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from loguru import logger

from app.core.auth import decode_token
from app.core.websocket import ws_manager
from app.core.orchestrator import Orchestrator
from app.memory.store import memory
from app.health.models import HealthEventDetector, extract_metrics

router = APIRouter()

# 全局orchestrator（在main.py中初始化后设置）
_orchestrator: Orchestrator | None = None


def set_orchestrator(orch: Orchestrator):
    global _orchestrator
    _orchestrator = orch


async def _verify_ws_token(token: str) -> str | None:
    """验证WebSocket连接的token，返回user_id或None"""
    try:
        payload = decode_token(token)
        if payload and payload.get("type") == "access":
            return payload.get("user_id")
    except Exception:
        pass
    return None


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket, token: str = Query(...)):
    """
    WebSocket对话端点

    连接方式: ws://host:port/api/v1/ws/chat?token=xxx
    """
    # 验证token
    user_id = await _verify_ws_token(token)
    if not user_id:
        await websocket.close(code=4001, reason="认证失败")
        return

    # 建立连接
    await ws_manager.connect(websocket, user_id)

    try:
        while True:
            # 接收消息
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws_manager.send_to_user(user_id, {
                    "type": "error",
                    "message": "无效的消息格式",
                })
                continue

            msg_type = data.get("type")

            # 心跳
            if msg_type == "ping":
                await ws_manager.send_to_user(user_id, {"type": "pong"})
                continue

            # 对话
            if msg_type == "chat":
                message = data.get("message", "")
                health_data = data.get("health_data")
                session_id = data.get("session_id", f"ws_{user_id}")

                if not message.strip():
                    continue

                try:
                    # 存入用户消息
                    await memory.add_message(user_id, "user", message, session_id)

                    # 处理健康数据
                    health_snapshot = None
                    health_events = []
                    if health_data:
                        metrics = extract_metrics(health_data)
                        await memory.update_metrics(user_id, metrics)
                        health_snapshot = memory._build_health_snapshot(metrics)
                        events = HealthEventDetector.detect(metrics, await memory.get_baseline(user_id))
                        health_events = events

                    # 获取上下文
                    history = await memory.get_history(user_id, session_id, limit=14)
                    profile = await memory.get_profile(user_id)

                    # 记忆碎片检索
                    from app.memory.fragments import fragment_store
                    memory_text = fragment_store.retrieve(user_id, message, top_k=5)

                    # 调度
                    if _orchestrator:
                        result = await _orchestrator.orchestrate(
                            user_id=user_id,
                            message=message,
                            history=history,
                            health_snapshot=health_snapshot,
                            health_events=health_events,
                            memory_text=memory_text,
                            session_id=session_id,
                        )

                        # 存入助手回复
                        await memory.add_message(user_id, "assistant", result["reply"], session_id)

                        # 发送完整回复
                        await ws_manager.send_chat_complete(
                            user_id=user_id,
                            reply=result["reply"],
                            engine=result["engine"],
                            intent=result["intent"],
                            blocks=result.get("blocks", []),
                        )

                        # 异步触发记忆提取
                        user_turns = await memory.count_user_turns(user_id, session_id)
                        if user_turns % 5 == 0 and user_turns > 0:
                            all_history = await memory.get_history(user_id, session_id, limit=10)
                            asyncio.create_task(
                                _safe_ws_extract(user_id, session_id, all_history)
                            )
                    else:
                        await ws_manager.send_to_user(user_id, {
                            "type": "error",
                            "message": "服务尚未就绪",
                        })

                except Exception as e:
                    logger.error(f"WebSocket chat error for {user_id}: {e}")
                    await ws_manager.send_to_user(user_id, {
                        "type": "error",
                        "message": "处理您的消息时遇到了问题，请稍后再试",
                    })

            # 未知消息类型
            else:
                await ws_manager.send_to_user(user_id, {
                    "type": "error",
                    "message": f"未知的消息类型: {msg_type}",
                })

    except WebSocketDisconnect:
        ws_manager.disconnect(user_id)
    except Exception as e:
        logger.error(f"WebSocket error for {user_id}: {e}")
        ws_manager.disconnect(user_id)


async def _safe_ws_extract(user_id: str, session_id: str, history: list):
    """WebSocket场景下的安全记忆提取"""
    try:
        from app.memory.extractor import extract_fragments, _update_constitution_from_fragments
        from app.memory.fragments import fragment_store

        frags = await extract_fragments(user_id, history, session_id)
        if frags:
            fragment_store.add_fragments(user_id, frags)
            await _update_constitution_from_fragments(user_id, frags)
            logger.info(f"WS记忆提取: {user_id} -> {len(frags)}条碎片")
    except Exception as e:
        logger.error(f"WS记忆提取失败: {user_id} -> {e}")
