"""事件推送接口"""

from fastapi import APIRouter

from app.api.schemas import EventPushRequest, EventPushResponse
from app.health.models import HealthEvent, HealthEventType, Severity
from app.memory.store import memory

router = APIRouter(prefix="/event", tags=["event"])


@router.post("/push", response_model=EventPushResponse)
async def push_event(req: EventPushRequest):
    """事件推送：接收外部事件（如手串触发），可触发 Agent 主动关怀"""
    # 1. 构建健康事件
    try:
        event_type = HealthEventType(req.event_type)
    except ValueError:
        return EventPushResponse(ok=False)

    try:
        severity = Severity(req.severity)
    except ValueError:
        severity = Severity.LOW

    from datetime import datetime
    event = HealthEvent(
        event_type=event_type,
        severity=severity,
        timestamp=datetime.now(),
        description=f"外部事件：{req.event_type}",
        data_summary=req.data,
    )

    # 2. 存储事件
    session_id = f"event-{req.user_id}"
    session = memory.get_session(session_id, req.user_id)
    memory.update_events(session_id, req.user_id, session.active_events + [event])

    # 3. 如果需要触发主动关怀
    care_message = None
    care_triggered = False
    if req.trigger_care and severity in (Severity.MODERATE, Severity.HIGH):
        care_triggered = True
        # P0 阶段：返回关怀消息模板
        # P1 阶段：会调用 Agent 生成个性化关怀消息
        care_templates = {
            HealthEventType.STRESS_DETECTED: "我注意到您最近压力有些大，要不要和我聊聊？",
            HealthEventType.ANXIETY_SUSPECTED: "感觉您的心跳有些快，是不是有些紧张？试试深呼吸，我陪您。",
            HealthEventType.POOR_SLEEP: "昨晚没休息好吧？今天别太累了，给自己一点放松的时间。",
        }
        care_message = care_templates.get(event_type, "我注意到您身体的一些变化，要和我聊聊吗？")

    return EventPushResponse(
        ok=True,
        care_triggered=care_triggered,
        care_message=care_message,
    )
