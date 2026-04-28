"""疗愈陪伴引擎"""

from app.core.llm import chat_with_system
from app.core.rag import rag_service
from app.engines.healing.prompts import HEALING_ENGINE_SYSTEM_PROMPT, HEALING_SNAPSHOT_TEMPLATE
from app.health.models import HealthMetrics, HealthEvent, UserHealthBaseline


def build_healing_snapshot(
    metrics: HealthMetrics | None = None,
    baseline: UserHealthBaseline | None = None,
    events: list[HealthEvent] | None = None,
    emotion_trend: str = "暂无数据",
    last_meditation: str = "暂无记录",
    anxiety_days: int = 0,
) -> str:
    """构建疗愈场景的身心状态快照"""

    if metrics is None:
        return "暂无用户身心数据"

    baseline = baseline or UserHealthBaseline(user_id="default")

    # 心率状态（疗愈视角：紧张程度）
    if metrics.heart_rate_avg is not None:
        hr = metrics.heart_rate_avg
        if hr > 90:
            hr_status = "偏快（可能有些紧张）"
        elif hr > 80:
            hr_status = "略快"
        elif hr < 55:
            hr_status = "偏慢（放松状态）"
        else:
            hr_status = "平稳"
        hr_value = f"{hr:.0f}次/分"
    else:
        hr_status = "未知"
        hr_value = "无数据"

    # HRV 状态（疗愈视角：压力水平）
    if metrics.hrv_sdnn is not None:
        hrv_ratio = metrics.hrv_sdnn / baseline.hrv_sdnn_baseline
        if hrv_ratio < 0.6:
            hrv_status = "很低（身体压力较大）"
        elif hrv_ratio < 0.8:
            hrv_status = "偏低（有些压力）"
        elif hrv_ratio < 1.0:
            hrv_status = "略低"
        else:
            hrv_status = "良好（放松状态）"
        hrv_value = f"{metrics.hrv_sdnn:.1f}ms"
    else:
        hrv_status = "未知"
        hrv_value = "无数据"

    # 睡眠状态（疗愈视角：情绪基础）
    if metrics.sleep_duration_hours is not None:
        sleep = metrics.sleep_duration_hours
        if sleep < 4:
            sleep_status = "严重不足（情绪基础薄弱）"
        elif sleep < 5:
            sleep_status = "不足（影响情绪稳定）"
        elif sleep < 6:
            sleep_status = "偏少"
        elif sleep < 7:
            sleep_status = "略少"
        else:
            sleep_status = "充足（情绪基础稳定）"
        sleep_hours = f"{sleep:.1f}"
    else:
        sleep_status = "未知"
        sleep_hours = "无数据"

    # 活跃事件
    if events:
        active_events = "；".join([e.description for e in events[:3]])
    else:
        active_events = "无"

    return HEALING_SNAPSHOT_TEMPLATE.format(
        emotion_trend=emotion_trend,
        last_meditation=last_meditation,
        anxiety_days=anxiety_days,
        heart_rate_status=hr_status,
        heart_rate_value=hr_value,
        hrv_status=hrv_status,
        hrv_value=hrv_value,
        sleep_status=sleep_status,
        sleep_hours=sleep_hours,
        active_events=active_events,
    )


async def healing_chat(
    user_message: str,
    user_id: str,
    history: list[dict] | None = None,
    healing_snapshot: str | None = None,
    light_mode: bool = False,
    memory_text: str = "",
) -> str:
    """疗愈陪伴引擎对话

    Args:
        user_message: 用户消息
        user_id: 用户 ID
        history: 对话历史
        healing_snapshot: 疗愈场景身心状态快照
        light_mode: 轻量模式（用于闲聊场景，减少知识注入）
        memory_text: 格式化后的记忆碎片文本

    Returns:
        引擎回复
    """
    system_prompt = HEALING_ENGINE_SYSTEM_PROMPT

    # 注入记忆碎片（在快照之前，让引擎先了解用户背景）
    if memory_text:
        system_prompt += f"\n\n{memory_text}"

    if healing_snapshot and healing_snapshot != "暂无用户身心数据":
        system_prompt += f"\n\n{healing_snapshot}"

    # 注入中医心理学知识库（轻量模式下不注入，保持自然闲聊感）
    if not light_mode:
        context_messages = history or []
        context_messages = context_messages + [{"role": "user", "content": user_message}]
        knowledge_section = rag_service.retrieve(
            messages=context_messages,
            engine_type="healing",
            max_chars=3000 if len(context_messages) <= 4 else 4000,
        )
        if knowledge_section:
            system_prompt += f"\n\n## 中医心理学参考知识\n{knowledge_section}"

    return await chat_with_system(
        system_prompt=system_prompt,
        user_message=user_message,
        history=history,
        temperature=0.75,  # 稍高一点，更有温度感
        max_tokens=600,    # 冥想引导需要更长
    )
