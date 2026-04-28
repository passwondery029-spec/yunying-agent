"""对话接口"""

import asyncio
import json
import uuid
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse
from loguru import logger

from app.api.schemas import ChatRequest, ChatResponse
from app.core.orchestrator import orchestrate, orchestrate_stream
from app.core.output_parser import parse_blocks
from app.core.auth import require_auth, TokenData
from app.memory.store import memory
from app.memory.fragments import fragment_store
from app.memory.extractor import extract_fragments
from app.health.models import HealthMetrics

router = APIRouter(prefix="/chat", tags=["chat"])

# 记忆提取触发间隔（对话轮数）
EXTRACTION_INTERVAL = 5

# P0-4: 用户级并发锁，防止同一用户并发请求导致状态混乱
_user_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# 全局后台任务集合，防止 asyncio.create_task 被 GC 回收
_background_tasks: set[asyncio.Task] = set()


def _create_background_task(coro) -> asyncio.Task:
    """创建后台任务并注册到全局集合，防止被GC回收"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _health_data_to_metrics(payload) -> HealthMetrics | None:
    """将 App 传入的 health_data 转为 HealthMetrics（如果提供了的话）"""
    if payload is None:
        return None
    has_data = any([
        payload.heart_rate is not None,
        payload.hrv is not None,
        payload.temperature is not None,
        payload.sleep_hours is not None,
        payload.steps is not None,
    ])
    if not has_data:
        return None

    return HealthMetrics(
        heart_rate_avg=payload.heart_rate,
        heart_rate_max=payload.heart_rate,
        heart_rate_min=payload.heart_rate,
        heart_rate_resting=payload.heart_rate,
        hrv_sdnn=payload.hrv,
        temperature_avg=payload.temperature,
        steps=payload.steps,
        sleep_duration_hours=payload.sleep_hours,
    )


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request, auth: TokenData = Depends(require_auth)):
    """对话接口：用户消息 → 意图路由 → 引擎处理 → 返回回复"""
    try:
        # 0. 从认证token获取用户身份（不可伪造）
        user_id = auth.user_id

        # 1. 获取或创建 session
        # 没传session_id时，用用户级固定session（确保对话历史连续）
        session_id = req.session_id or f"default_{user_id}"

        # P0-4: 用户级并发控制 —— 同一用户同一时刻只处理一个请求
        lock = _user_locks[user_id]
        if lock.locked():
            raise HTTPException(
                status_code=429,
                detail="您的上一个请求正在处理中，请稍后再试",
            )

        async with lock:
            # 2. 加载健康数据：优先使用请求附带的，否则从数据库读取
            from app.core.database import get_latest_metrics as db_get_latest_metrics
            if req.health_data is not None:
                realtime_metrics = _health_data_to_metrics(req.health_data)
                if realtime_metrics is not None:
                    memory.update_metrics(user_id, realtime_metrics)
                # 更新情绪趋势和冥想信息到用户画像
                profile_updates = {}
                if req.health_data.emotion_trend:
                    profile_updates["emotion_trend"] = req.health_data.emotion_trend
                if req.health_data.last_meditation:
                    profile_updates["last_meditation"] = req.health_data.last_meditation
                if profile_updates:
                    memory.update_profile(user_id, **profile_updates)
            else:
                # 请求未附带健康数据 → 从数据库加载上次手动填报的数据
                saved_metrics = await db_get_latest_metrics(user_id)
                if saved_metrics:
                    realtime_metrics = HealthMetrics(
                        heart_rate_avg=saved_metrics.get("heart_rate"),
                        heart_rate_resting=saved_metrics.get("heart_rate"),
                        hrv_sdnn=saved_metrics.get("hrv"),
                        temperature_avg=saved_metrics.get("skin_temp"),
                        steps=saved_metrics.get("steps"),
                        sleep_duration_hours=saved_metrics.get("sleep_hours"),
                    )
                    memory.update_metrics(user_id, realtime_metrics)

            # 3. 记录用户消息
            await memory.add_message(session_id, "user", req.message, user_id)

            # 4. 构建上下文
            history = await memory.get_history(session_id, user_id, limit=10)
            # 去掉最后一条（刚加的用户消息），避免重复
            history = history[:-1] if history else []

            # 获取用户画像（用于构建各引擎快照）
            profile = await memory.get_profile(user_id)

            # 获取活跃健康事件
            session = memory.get_session(session_id, user_id)
            health_events = session.active_events

            # 4.5 检索记忆碎片，注入到上下文中
            memory_fragments = fragment_store.retrieve(
                user_id=user_id,
                query=req.message,
                top_k=5,
            )
            memory_text = fragment_store.format_fragments_for_prompt(memory_fragments)

            # 5. 调度到引擎（传入记忆碎片）
            result = await orchestrate(
                user_message=req.message,
                user_id=user_id,
                history=history,
                health_events=health_events,
                profile=profile,
                memory_text=memory_text,
            )

            # 6. 记录助手回复（用原始文本）
            await memory.add_message(session_id, "assistant", result.reply, user_id)

            # 7. 解析结构化输出
            clean_reply, blocks = parse_blocks(result.reply)

            # 8. 如果有产品推荐块，提取到顶层字段
            product_rec = None
            for block in blocks:
                if hasattr(block, 'type') and block.type == 'product':
                    product_rec = {
                        "name": block.name,
                        "description": block.description,
                        "price": block.price,
                        "tcm_rationale": block.tcm_rationale,
                    }
                    # 更新用户画像：已推荐
                    profile.already_recommended = True
                    break

            # 9. 记忆碎片提取（每N轮触发，异步执行不阻塞）
            user_turns = len([m for m in history if m.get("role") == "user"])
            if user_turns > 0 and user_turns % EXTRACTION_INTERVAL == 0:
                # 取最近N轮对话用于提取
                all_history = await memory.get_history(
                    session_id, user_id, limit=EXTRACTION_INTERVAL * 2
                )
                if all_history and len(all_history) >= 4:

                    async def _safe_extract():
                        try:
                            await extract_fragments(
                                user_id=user_id,
                                session_messages=all_history,
                                source_session=session_id,
                            )
                            # 体质更新已在extractor.extract_fragments内部完成
                        except Exception as e:
                            logger.warning(f"记忆提取异步任务失败: {e}")

                    _create_background_task(_safe_extract())
                    logger.debug(f"用户 {user_id}: 触发记忆提取（第{user_turns}轮）")

            # 9.5 体质自动推断（当体质未测评且对话>=5轮时触发）
            if (
                profile.constitution in ("未测评", None, "")
                and user_turns >= 5
                and user_turns % 5 == 0
            ):
                from app.memory.extractor import infer_constitution_from_dialogue

                recent = await memory.get_history(session_id, user_id, limit=10)

                async def _safe_infer():
                    try:
                        await infer_constitution_from_dialogue(user_id, recent)
                    except Exception as e:
                        logger.warning(f"体质推断失败: {e}")

                _create_background_task(_safe_infer())

            # 10. 返回结果
            return ChatResponse(
                reply=clean_reply,
                blocks=[block.model_dump() for block in blocks],
                engine=result.engine,
                intent=result.intent.value,
                session_id=session_id,
                suggested_actions=result.suggested_actions or [],
                product_recommendation=product_rec,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"对话处理失败: {e}")
        raise HTTPException(status_code=500, detail="对话处理失败，请稍后再试")


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request, auth: TokenData = Depends(require_auth)):
    """流式对话接口：SSE 逐 token 返回

    前端通过 fetch + ReadableStream 接收，
    每条 SSE data 是一个 JSON: {"text": "..."} 或 {"done": true, "meta": {...}}
    """
    user_id = auth.user_id
    session_id = req.session_id or f"default_{user_id}"

    # 并发控制
    lock = _user_locks[user_id]
    if lock.locked():
        raise HTTPException(status_code=429, detail="您的上一个请求正在处理中，请稍后再试")

    async def _generate():
        try:
            async with lock:
                # 加载健康数据
                from app.core.database import get_latest_metrics as db_get_latest_metrics
                if req.health_data is not None:
                    realtime_metrics = _health_data_to_metrics(req.health_data)
                    if realtime_metrics is not None:
                        memory.update_metrics(user_id, realtime_metrics)
                    profile_updates = {}
                    if req.health_data.emotion_trend:
                        profile_updates["emotion_trend"] = req.health_data.emotion_trend
                    if req.health_data.last_meditation:
                        profile_updates["last_meditation"] = req.health_data.last_meditation
                    if profile_updates:
                        memory.update_profile(user_id, **profile_updates)
                else:
                    saved_metrics = await db_get_latest_metrics(user_id)
                    if saved_metrics:
                        realtime_metrics = HealthMetrics(
                            heart_rate_avg=saved_metrics.get("heart_rate"),
                            heart_rate_resting=saved_metrics.get("heart_rate"),
                            hrv_sdnn=saved_metrics.get("hrv"),
                            temperature_avg=saved_metrics.get("skin_temp"),
                            steps=saved_metrics.get("steps"),
                            sleep_duration_hours=saved_metrics.get("sleep_hours"),
                        )
                        memory.update_metrics(user_id, realtime_metrics)

                # 记录用户消息
                await memory.add_message(session_id, "user", req.message, user_id)

                # 构建上下文
                history = await memory.get_history(session_id, user_id, limit=10)
                history = history[:-1] if history else []
                profile = await memory.get_profile(user_id)
                session = memory.get_session(session_id, user_id)
                health_events = session.active_events

                # 记忆碎片
                memory_fragments = fragment_store.retrieve(
                    user_id=user_id, query=req.message, top_k=5,
                )
                memory_text = fragment_store.format_fragments_for_prompt(memory_fragments)

                # 流式生成
                full_reply = ""
                async for chunk in orchestrate_stream(
                    user_message=req.message,
                    user_id=user_id,
                    history=history,
                    health_events=health_events,
                    profile=profile,
                    memory_text=memory_text,
                ):
                    full_reply += chunk
                    yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"

                # 记录完整回复
                await memory.add_message(session_id, "assistant", full_reply, user_id)

                # 后台：记忆提取
                user_turns = len([m for m in history if m.get("role") == "user"])
                if user_turns > 0 and user_turns % EXTRACTION_INTERVAL == 0:
                    all_history = await memory.get_history(
                        session_id, user_id, limit=EXTRACTION_INTERVAL * 2
                    )
                    if all_history and len(all_history) >= 4:
                        async def _safe_extract():
                            try:
                                await extract_fragments(
                                    user_id=user_id,
                                    session_messages=all_history,
                                    source_session=session_id,
                                )
                            except Exception as e:
                                logger.warning(f"记忆提取异步任务失败: {e}")
                        _create_background_task(_safe_extract())

                # 后台：体质推断
                if (
                    profile.constitution in ("未测评", None, "")
                    and user_turns >= 5
                    and user_turns % 5 == 0
                ):
                    from app.memory.extractor import infer_constitution_from_dialogue
                    recent = await memory.get_history(session_id, user_id, limit=10)
                    async def _safe_infer():
                        try:
                            await infer_constitution_from_dialogue(user_id, recent)
                        except Exception as e:
                            logger.warning(f"体质推断失败: {e}")
                    _create_background_task(_safe_infer())

                # 发送完成信号
                clean_reply, blocks = parse_blocks(full_reply)
                yield f"data: {json.dumps({'done': True, 'meta': {'reply': clean_reply, 'blocks': [b.model_dump() for b in blocks]}}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.error(f"流式对话失败: {e}")
            yield f"data: {json.dumps({'error': '对话处理失败，请稍后再试'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
