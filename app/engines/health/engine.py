"""健康引导引擎"""

from datetime import datetime
from typing import AsyncGenerator

from app.core.llm import chat_with_system, chat_stream_with_system
from app.core.rag import rag_service
from app.engines.health.prompts import HEALTH_ENGINE_SYSTEM_PROMPT, HEALTH_SNAPSHOT_TEMPLATE
from app.health.models import HealthMetrics, HealthEvent, UserHealthBaseline


def build_health_snapshot(
    metrics: HealthMetrics | None = None,
    baseline: UserHealthBaseline | None = None,
    events: list[HealthEvent] | None = None,
    emotion_trend: str = "暂无数据",
    last_meditation: str = "暂无记录",
) -> str:
    """构建健康快照文本，注入对话上下文"""

    if metrics is None:
        return "暂无用户健康数据"

    baseline = baseline or UserHealthBaseline(user_id="default")

    # 心率状态判断
    if metrics.heart_rate_avg is not None:
        hr = metrics.heart_rate_avg
        if hr < 60:
            hr_status = "偏低"
        elif hr > 100:
            hr_status = "偏高"
        else:
            hr_status = "正常"
        hr_value = f"{hr:.0f}次/分"
    else:
        hr_status = "未知"
        hr_value = "无数据"

    # HRV 状态判断
    if metrics.hrv_sdnn is not None:
        hrv_ratio = metrics.hrv_sdnn / baseline.hrv_sdnn_baseline
        if hrv_ratio < 0.7:
            hrv_status = "偏低（压力较大）"
        elif hrv_ratio < 0.9:
            hrv_status = "略低"
        else:
            hrv_status = "正常"
        hrv_value = f"{metrics.hrv_sdnn:.1f}ms"
    else:
        hrv_status = "未知"
        hrv_value = "无数据"

    # 体温状态
    if metrics.temperature_avg is not None:
        temp = metrics.temperature_avg
        low, high = baseline.temperature_range
        if temp > high:
            temp_status = "偏高"
        elif temp < low:
            temp_status = "偏低"
        else:
            temp_status = "正常"
        temp_value = f"{temp:.1f}°C"
    else:
        temp_status = "未知"
        temp_value = "无数据"

    # 睡眠状态
    if metrics.sleep_duration_hours is not None:
        sleep = metrics.sleep_duration_hours
        if sleep < 5:
            sleep_status = "严重不足"
        elif sleep < 6:
            sleep_status = "不足"
        elif sleep < 7:
            sleep_status = "略少"
        else:
            sleep_status = "正常"
        sleep_hours = f"{sleep:.1f}"
    else:
        sleep_status = "未知"
        sleep_hours = "无数据"

    # 活动状态
    if metrics.steps is not None:
        steps = metrics.steps
        if steps < 2000:
            activity_status = "久坐"
        elif steps < 5000:
            activity_status = "偏少"
        elif steps < 8000:
            activity_status = "适中"
        else:
            activity_status = "充足"
        steps_str = f"{steps}"
    else:
        activity_status = "未知"
        steps_str = "无数据"

    # 活跃事件
    if events:
        active_events = "；".join([e.description for e in events[:3]])
    else:
        active_events = "无异常"

    return HEALTH_SNAPSHOT_TEMPLATE.format(
        heart_rate_status=hr_status,
        heart_rate_value=hr_value,
        hrv_status=hrv_status,
        hrv_value=hrv_value,
        temperature_status=temp_status,
        temperature_value=temp_value,
        sleep_status=sleep_status,
        sleep_hours=sleep_hours,
        activity_status=activity_status,
        steps=steps_str,
        emotion_trend=emotion_trend,
        last_meditation=last_meditation,
        active_events=active_events,
    )


async def health_chat(
    user_message: str,
    user_id: str,
    history: list[dict] | None = None,
    health_snapshot: str | None = None,
    memory_text: str = "",
) -> str:
    """健康引导引擎对话

    Args:
        user_message: 用户消息
        user_id: 用户 ID
        history: 对话历史
        health_snapshot: 健康快照文本
        memory_text: 格式化后的记忆碎片文本

    Returns:
        引擎回复
    """
    # 组装系统提示
    system_prompt = HEALTH_ENGINE_SYSTEM_PROMPT

    # 注入记忆碎片（在快照之前，让引擎先了解用户背景）
    if memory_text:
        system_prompt += f"\n\n{memory_text}"

    if health_snapshot and health_snapshot != "暂无用户健康数据":
        system_prompt += f"\n\n{health_snapshot}"

    # 注入中医心理学知识库（基于最近几轮对话上下文检索）
    context_messages = history or []
    context_messages = context_messages + [{"role": "user", "content": user_message}]
    knowledge_section = rag_service.retrieve(
        messages=context_messages,
        engine_type="health",
        max_chars=3000 if len(context_messages) <= 4 else 4000,
    )
    if knowledge_section:
        system_prompt += f"\n\n## 中医心理学参考知识\n{knowledge_section}"

    return await chat_with_system(
        system_prompt=system_prompt,
        user_message=user_message,
        history=history,
        temperature=0.7,
        max_tokens=512,
    )


async def health_chat_stream(
    user_message: str,
    user_id: str,
    history: list[dict] | None = None,
    health_snapshot: str | None = None,
    memory_text: str = "",
) -> AsyncGenerator[str, None]:
    """健康引导引擎流式对话"""
    system_prompt = HEALTH_ENGINE_SYSTEM_PROMPT

    if memory_text:
        system_prompt += f"\n\n{memory_text}"

    if health_snapshot and health_snapshot != "暂无用户健康数据":
        system_prompt += f"\n\n{health_snapshot}"

    context_messages = history or []
    context_messages = context_messages + [{"role": "user", "content": user_message}]
    knowledge_section = rag_service.retrieve(
        messages=context_messages,
        engine_type="health",
        max_chars=3000 if len(context_messages) <= 4 else 4000,
    )
    if knowledge_section:
        system_prompt += f"\n\n## 中医心理学参考知识\n{knowledge_section}"

    async for chunk in chat_stream_with_system(
        system_prompt=system_prompt,
        user_message=user_message,
        history=history,
        temperature=0.7,
        max_tokens=512,
    ):
        yield chunk
