"""健康数据上报接口"""

from datetime import datetime
from fastapi import APIRouter, Depends

from app.api.schemas import HealthUploadRequest, HealthUploadResponse
from app.core.auth import require_auth, TokenData
from app.health.models import (
    DataType, HealthDataPoint, HealthMetrics,
    HealthEventDetector, UserHealthBaseline, extract_metrics,
)
from app.memory.store import memory

router = APIRouter(prefix="/health", tags=["health"])


@router.post("/upload", response_model=HealthUploadResponse)
async def upload_health_data(req: HealthUploadRequest, auth: TokenData = Depends(require_auth)):
    """健康数据上报：接收手串数据 → 特征提取 → 事件检测 → 存储更新"""
    # 强制使用认证用户的ID
    req.user_id = auth.user_id

    # 1. 解析数据类型
    try:
        data_type = DataType(req.data_type)
    except ValueError:
        return HealthUploadResponse(ok=False, events_detected=0)

    # 2. 解析数据点
    values = []
    for v in req.values:
        try:
            ts = v.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            values.append(HealthDataPoint(timestamp=ts, value=float(v.get("value", 0))))
        except (ValueError, TypeError, KeyError):
            continue

    if not values:
        return HealthUploadResponse(ok=False, events_detected=0)

    # 3. 特征提取
    metrics = extract_metrics(data_type, values)

    # 4. 更新用户健康指标
    await memory.update_metrics(req.user_id, metrics)

    # 5. 事件检测
    profile = await memory.get_profile(req.user_id)
    detector = HealthEventDetector(baseline=profile.baseline)
    events = detector.detect(metrics)

    # 6. 更新活跃事件
    session_id = f"health-{req.user_id}"
    await memory.update_events(session_id, req.user_id, events)

    # 7. 返回结果
    return HealthUploadResponse(
        ok=True,
        events_detected=len(events),
        event_descriptions=[e.description for e in events],
    )
