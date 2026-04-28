"""手动健康数据上报 — 用户无硬件时手动填写"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from loguru import logger

from app.core.auth import require_auth, TokenData
from app.health.models import HealthMetrics, UserHealthBaseline, HealthEventDetector
from app.memory.store import memory as memory_store
from app.core.database import save_metrics, get_latest_metrics, upsert_profile, get_profile

router = APIRouter(prefix="/health", tags=["health"])


class ManualHealthInput(BaseModel):
    """用户手动填写的健康数据"""
    # 心率（静息）
    heart_rate: float | None = Field(None, description="当前静息心率(次/分)", ge=40, le=200)
    # HRV（心率变异性）
    hrv: float | None = Field(None, description="心率变异性SDNN(ms)", ge=10, le=200)
    # 昨晚睡眠
    sleep_hours: float | None = Field(None, description="昨晚睡眠时长(小时)", ge=0, le=24)
    # 睡眠质量自评
    sleep_quality: float | None = Field(None, description="睡眠质量自评(0-100)", ge=0, le=100)
    # 今日步数
    steps: int | None = Field(None, description="今日步数", ge=0, le=100000)
    # 体温
    temperature: float | None = Field(None, description="当前体温(°C)", ge=35.0, le=42.0)
    # 压力自评
    stress_level: str | None = Field(None, description="压力自评: low/moderate/high")
    # 当前心情
    mood: str | None = Field(None, description="当前心情描述，如：平静/焦虑/低落/开心/烦躁")


class HealthProfileResponse(BaseModel):
    """健康档案响应"""
    has_data: bool
    metrics: dict | None = None
    baseline: dict | None = None
    events: list[dict] = []
    last_updated: str | None = None
    mood: str | None = None


@router.post("/manual", response_model=HealthProfileResponse)
async def submit_manual_health(
    data: ManualHealthInput,
    auth: TokenData = Depends(require_auth),
):
    """用户手动提交健康数据"""
    user_id = auth.user_id
    store = memory_store

    # 1. 构建 HealthMetrics
    metrics = HealthMetrics(
        heart_rate_avg=data.heart_rate,
        heart_rate_resting=data.heart_rate,
        hrv_sdnn=data.hrv,
        sleep_duration_hours=data.sleep_hours,
        sleep_quality_score=data.sleep_quality,
        steps=data.steps,
        temperature_avg=data.temperature,
    )

    # 2. 持久化到数据库
    metrics_dict = {
        "heart_rate": data.heart_rate,
        "hrv": data.hrv,
        "sleep_hours": data.sleep_hours,
        "sleep_quality": data.sleep_quality,
        "steps": data.steps,
        "skin_temp": data.temperature,
        "stress_level": data.stress_level,
        "mood": data.mood,
    }
    await save_metrics(user_id, metrics_dict)

    # 3. 更新内存缓存
    store.update_metrics(user_id, metrics)

    # 4. 检测健康事件
    profile_data = await get_profile(user_id)
    baseline = None
    if profile_data and profile_data.get("baseline"):
        baseline = UserHealthBaseline(user_id=user_id, **profile_data["baseline"])
    if not baseline:
        baseline = UserHealthBaseline(user_id=user_id)

    detector = HealthEventDetector(baseline=baseline)
    events = detector.detect(metrics)
    event_dicts = [
        {
            "event_type": e.event_type.value,
            "severity": e.severity.value,
            "description": e.description,
        }
        for e in events
    ]

    # 5. 如果有心情，更新到用户画像
    if data.mood:
        from datetime import datetime as dt
        current_profile = await get_profile(user_id)
        emotion_trend = current_profile.get("emotion_trend", []) if current_profile else []
        emotion_trend.append({"mood": data.mood, "time": dt.now().isoformat()})
        # 只保留最近20条
        emotion_trend = emotion_trend[-20:]
        await upsert_profile(user_id, emotion_trend=emotion_trend)

    # 6. 如果用户有压力自评，更新 baseline
    if data.stress_level:
        stress_map = {"low": 0.3, "moderate": 0.6, "high": 0.9}
        # 暂存到 metrics dict 中
        metrics_dict["stress_score"] = stress_map.get(data.stress_level, 0.5)

    logger.info(f"用户 {user_id} 手动提交健康数据: {metrics_dict}")

    return HealthProfileResponse(
        has_data=True,
        metrics=metrics_dict,
        baseline={
            "resting_hr": baseline.resting_hr,
            "hrv_sdnn_baseline": baseline.hrv_sdnn_baseline,
            "sleep_avg_hours": baseline.sleep_avg_hours,
            "daily_steps_avg": baseline.daily_steps_avg,
        },
        events=event_dicts,
        mood=data.mood,
    )


@router.get("/profile", response_model=HealthProfileResponse)
async def get_health_profile(
    auth: TokenData = Depends(require_auth),
):
    """获取用户健康档案"""
    user_id = auth.user_id
    # 从数据库获取最新指标
    metrics = await get_latest_metrics(user_id)
    profile = await get_profile(user_id)

    baseline = None
    if profile and profile.get("baseline"):
        baseline = profile["baseline"]

    # 如果有指标，检测事件
    events = []
    if metrics:
        health_metrics = HealthMetrics(
            heart_rate_avg=metrics.get("heart_rate"),
            heart_rate_resting=metrics.get("heart_rate"),
            hrv_sdnn=metrics.get("hrv"),
            sleep_duration_hours=metrics.get("sleep_hours"),
            steps=metrics.get("steps"),
            temperature_avg=metrics.get("skin_temp"),
        )
        bl = UserHealthBaseline(user_id=user_id, **baseline) if baseline else UserHealthBaseline(user_id=user_id)
        detector = HealthEventDetector(baseline=bl)
        detected = detector.detect(health_metrics)
        events = [
            {"event_type": e.event_type.value, "severity": e.severity.value, "description": e.description}
            for e in detected
        ]

    return HealthProfileResponse(
        has_data=metrics is not None,
        metrics=metrics,
        baseline=baseline,
        events=events,
        last_updated=metrics.get("recorded_at") if metrics else None,
        mood=None,
    )
