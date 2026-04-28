"""API 请求/响应模型"""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


# === 对话接口 ===

class HealthDataPayload(BaseModel):
    """App 端传入的实时健康数据（对话时一次性注入）"""
    heart_rate: float | None = None           # 当前心率 bpm
    hrv: float | None = None                  # 当前 HRV (SDNN) ms
    temperature: float | None = None          # 当前体温 °C
    sleep_hours: float | None = None          # 昨晚睡眠时长
    steps: int | None = None                  # 今日步数
    emotion_trend: str | None = None          # 情绪趋势描述
    last_meditation: str | None = None        # 上次冥想时间


# === 结构化响应内容块 ===

class TextBlock(BaseModel):
    """文字段落"""
    type: Literal["text"] = "text"
    content: str                              # 文字内容（支持 Markdown）


class ActionBlock(BaseModel):
    """可操作建议"""
    type: Literal["action"] = "action"
    label: str                                # 按钮文字（如"开始冥想""查看详情"）
    action: str                               # 动作标识（如"start_meditation""view_product"）
    params: dict = Field(default_factory=dict)  # 动作参数


class MeditationBlock(BaseModel):
    """冥想引导步骤"""
    type: Literal["meditation"] = "meditation"
    title: str                                # 引导标题
    steps: list[str]                          # 步骤列表
    duration_minutes: int = 5                 # 建议时长
    style: str = "breathing"                  # breathing / body_scan / intention / visualization


class ProductBlock(BaseModel):
    """产品推荐卡片"""
    type: Literal["product"] = "product"
    name: str                                 # 产品名称
    description: str                          # 一句话描述
    price: str | None = None                  # 价格（如有）
    image_url: str | None = None              # 产品图片（如有）
    buy_url: str | None = None                # 购买链接（如有）
    tcm_rationale: str | None = None          # 中医推荐理由


class HealthTipBlock(BaseModel):
    """健康提示卡片"""
    type: Literal["health_tip"] = "health_tip"
    title: str                                # 提示标题
    content: str                              # 提示内容
    severity: str = "info"                    # info / warning / alert
    icon: str = "💡"                          # 显示图标


# 所有内容块的联合类型
ContentBlock = TextBlock | ActionBlock | MeditationBlock | ProductBlock | HealthTipBlock


class ChatRequest(BaseModel):
    """对话请求"""
    user_id: str = "anonymous"                       # 认证用户可省略，由token决定
    message: str
    session_id: str | None = None
    channel: str = "api"  # app / mini_program / web / api
    health_data: HealthDataPayload | None = None  # App 端传入的实时健康数据


class ChatResponse(BaseModel):
    """对话响应（结构化）"""
    reply: str                                    # 纯文本回复（向后兼容）
    blocks: list[ContentBlock] = Field(default_factory=list)  # 结构化内容块
    engine: str
    intent: str
    session_id: str
    suggested_actions: list[dict] = Field(default_factory=list)
    product_recommendation: dict | None = None


# === 健康数据上报 ===

class HealthUploadRequest(BaseModel):
    """健康数据上报请求"""
    user_id: str
    device_id: str
    data_type: str  # heart_rate / hrv / temperature / activity / sleep
    values: list[dict]  # [{"timestamp": "ISO8601", "value": number}]
    metadata: dict = Field(default_factory=dict)


class HealthUploadResponse(BaseModel):
    """健康数据上报响应"""
    ok: bool
    events_detected: int = 0
    event_descriptions: list[str] = Field(default_factory=list)


# === 事件推送 ===

class EventPushRequest(BaseModel):
    """事件推送请求"""
    user_id: str
    event_type: str  # stress_detected / anxiety_suspected / poor_sleep / meditation_done
    severity: str = "low"  # low / moderate / high
    data: dict = Field(default_factory=dict)
    trigger_care: bool = True


class EventPushResponse(BaseModel):
    """事件推送响应"""
    ok: bool
    care_triggered: bool = False
    care_message: str | None = None


# === 用户画像 ===

class UserProfileResponse(BaseModel):
    """用户画像响应"""
    user_id: str
    constitution: str
    main_concerns: list[str]
    emotion_trend: str
    healing_progress: str
    last_meditation: str
    last_interaction: datetime | None = None


# === 通用 ===

class HealthCheckResponse(BaseModel):
    """健康检查响应"""
    status: str = "ok"
    version: str = "0.2.0"
