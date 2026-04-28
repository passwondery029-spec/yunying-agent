"""
主动关怀引擎
- 健康异常事件 → 生成关怀消息 → 推送
- 定时关怀（季节变换提醒、作息提醒）
- 情绪持续低落 → 主动问候

关怀类型:
  - health_alert: 健康指标异常（心率过高/HRV过低/睡眠不足）
  - emotional_care: 情绪关怀（连续焦虑/低落）
  - meditation_reminder: 冥想提醒（睡前/午间）
  - seasonal_tip: 四季养生提醒
"""
import asyncio
from datetime import datetime, time
from typing import Optional
from loguru import logger

from app.core.websocket import ws_manager
from app.core.llm import chat
from app.memory.store import memory
from app.memory.fragments import fragment_store
from app.health.models import HealthMetrics, HealthEvent, HealthEventDetector


# 关怀消息模板（降级时使用，不需要LLM）
CARE_TEMPLATES = {
    "high_heart_rate": {
        "care_type": "health_alert",
        "severity": "warning",
        "template": "检测到你的心率偏高（{value}bpm），试着找个安静的地方坐下，做几次深呼吸。如果持续不适，建议及时就医。",
    },
    "low_hrv": {
        "care_type": "health_alert",
        "severity": "info",
        "template": "你的压力恢复指数（HRV）偏低（{value}），身体可能正在承受较大压力。建议今天给自己安排一些放松时间。",
    },
    "poor_sleep": {
        "care_type": "health_alert",
        "severity": "info",
        "template": "昨晚睡眠只有{value}小时，睡眠不足会影响情绪和免疫力。今晚试试提前半小时上床？",
    },
    "high_stress": {
        "care_type": "emotional_care",
        "severity": "info",
        "template": "今天的数据显示你压力较大，辛苦了。记得给自己一点喘息的空间，哪怕只是5分钟的深呼吸。",
    },
    "meditation_evening": {
        "care_type": "meditation_reminder",
        "severity": "info",
        "template": "忙碌了一天，睡前做个简短的冥想可以帮助你更好地入睡。需要我引导你吗？",
    },
}

# 关怀冷却时间（秒），避免频繁推送
CARE_COOLDOWNS: dict[str, datetime] = {}
COOLDOWN_SECONDS = 3600  # 1小时内同一类型不重复推送


def _is_cooled_down(user_id: str, care_key: str) -> bool:
    """检查是否在冷却期内"""
    key = f"{user_id}:{care_key}"
    last_sent = CARE_COOLDOWNS.get(key)
    if last_sent is None:
        return True
    elapsed = (datetime.now() - last_sent).total_seconds()
    return elapsed > COOLDOWN_SECONDS


def _mark_sent(user_id: str, care_key: str):
    """标记已发送，开始冷却"""
    key = f"{user_id}:{care_key}"
    CARE_COOLDOWNS[key] = datetime.now()


async def process_health_events(user_id: str, events: list[HealthEvent]):
    """
    处理健康事件，生成并推送关怀消息

    Args:
        user_id: 用户ID
        events: 健康事件列表
    """
    for event in events:
        care_key = f"{event.event_type}"

        # 冷却检查
        if not _is_cooled_down(user_id, care_key):
            continue

        # 用户不在线则跳过（App端会通过其他方式获取）
        if not ws_manager.is_connected(user_id):
            continue

        # 获取模板
        template = CARE_TEMPLATES.get(event.event_type)

        if template:
            # 使用模板生成消息
            message = template["template"].format(value=_format_event_value(event))
            care_type = template["care_type"]
            severity = template["severity"]
        else:
            # 无模板时用LLM生成
            message = await _generate_care_message(user_id, event)
            care_type = "health_alert"
            severity = "warning" if event.severity == "HIGH" else "info"

        if message:
            # 推送关怀消息
            sent = await ws_manager.send_care_message(
                user_id=user_id,
                message=message,
                care_type=care_type,
                severity=severity,
            )
            if sent:
                _mark_sent(user_id, care_key)
                logger.info(f"关怀推送: {user_id} <- {care_type} [{severity}] {message[:50]}")


async def _generate_care_message(user_id: str, event: HealthEvent) -> str:
    """用LLM生成个性化关怀消息"""
    try:
        # 获取用户画像和记忆
        profile = await memory.get_profile(user_id)
        fragments = fragment_store.retrieve(user_id, event.event_type, top_k=3)

        constitution = profile.constitution if profile and profile.constitution != "未测评" else ""
        memory_context = f"\n用户记忆：{fragments}" if fragments else ""
        constitution_context = f"\n用户体质：{constitution}" if constitution else ""

        system_prompt = f"""你是云英AI，一个温暖贴心的健康陪伴助手。你检测到用户的健康数据出现了异常，需要主动发一条关怀消息。

规则：
1. 语气温暖、关心，像朋友一样自然
2. 给出1个具体可行的建议（呼吸法/穴位按摩/休息提醒等）
3. 不要过于紧张，避免"建议立即就医"除非真的很严重
4. 控制在2-3句话
5. 如果有体质信息，可以结合体质给建议{constitution_context}{memory_context}

当前异常事件：{event.event_type}（严重程度：{event.severity}）
详情：{event.description}"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "请生成关怀消息"},
        ]

        result = await chat(messages, temperature=0.7, max_tokens=150)
        return result.strip()

    except Exception as e:
        logger.error(f"生成关怀消息失败: {e}")
        return "检测到你的身体状态有些异常，记得关注一下自己的感受。如果需要帮助，随时告诉我。"


def _format_event_value(event: HealthEvent) -> str:
    """从事件描述中提取数值"""
    desc = event.description or ""
    # 尝试提取数值
    import re
    match = re.search(r'(\d+\.?\d*)', desc)
    return match.group(1) if match else "异常"


async def send_scheduled_care(user_id: str, care_type: str = "meditation_reminder"):
    """
    发送定时关怀消息

    场景：
    - meditation_evening: 睡前冥想提醒（21:00-22:00）
    - seasonal_tip: 季节养生提醒
    """
    if not ws_manager.is_connected(user_id):
        return

    if not _is_cooled_down(user_id, f"scheduled_{care_type}"):
        return

    template = CARE_TEMPLATES.get(care_type)
    if template:
        message = template["template"]
    else:
        message = "是时候给自己一点关爱了，需要我陪你做个放松练习吗？"

    sent = await ws_manager.send_care_message(
        user_id=user_id,
        message=message,
        care_type=care_type,
        severity="info",
    )
    if sent:
        _mark_sent(user_id, f"scheduled_{care_type}")
