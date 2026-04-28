"""健康数据模型与特征提取"""

from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


# === 数据模型 ===

class DataType(str, Enum):
    """手串数据类型"""
    HEART_RATE = "heart_rate"
    HRV = "hrv"
    TEMPERATURE = "temperature"
    ACTIVITY = "activity"
    SLEEP = "sleep"


class HealthDataPoint(BaseModel):
    """单条健康数据"""
    timestamp: datetime
    value: float


class HealthUpload(BaseModel):
    """健康数据上报"""
    user_id: str
    device_id: str
    data_type: DataType
    values: list[HealthDataPoint]
    metadata: dict = Field(default_factory=dict)


# === 特征提取 ===

class HealthMetrics(BaseModel):
    """提取后的健康指标"""
    # 心率
    heart_rate_avg: float | None = None
    heart_rate_max: float | None = None
    heart_rate_min: float | None = None
    heart_rate_resting: float | None = None

    # HRV
    hrv_sdnn: float | None = None      # SDNN，心率变异性标准差
    hrv_rmssd: float | None = None     # RMSSD，相邻RR间期差值的均方根

    # 体温
    temperature_avg: float | None = None

    # 活动量
    steps: int | None = None
    active_minutes: int | None = None

    # 睡眠
    sleep_duration_hours: float | None = None
    sleep_quality_score: float | None = None  # 0-100


class Severity(str, Enum):
    """事件严重程度"""
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class HealthEventType(str, Enum):
    """健康事件类型"""
    STRESS_DETECTED = "stress_detected"
    ANXIETY_SUSPECTED = "anxiety_suspected"
    POOR_SLEEP = "poor_sleep"
    LOW_ACTIVITY = "low_activity"
    ABNORMAL_HEART_RATE = "abnormal_heart_rate"
    FEVER_SUSPECTED = "fever_suspected"
    MEDITATION_DONE = "meditation_done"


class HealthEvent(BaseModel):
    """检测到的健康事件"""
    event_type: HealthEventType
    severity: Severity
    timestamp: datetime
    description: str
    data_summary: dict = Field(default_factory=dict)
    trigger_care: bool = True  # 是否触发 Agent 主动关怀


class UserHealthBaseline(BaseModel):
    """用户健康基线（个性化）"""
    user_id: str
    resting_hr: float = 72.0        # 静息心率
    hrv_sdnn_baseline: float = 50.0  # HRV 基线
    sleep_avg_hours: float = 7.0     # 平均睡眠
    temperature_range: tuple[float, float] = (36.0, 37.2)  # 正常体温范围
    daily_steps_avg: float = 5000.0  # 日均步数


# === 事件检测器 ===

class HealthEventDetector:
    """从健康指标中检测异常事件"""

    def __init__(self, baseline: UserHealthBaseline | None = None):
        self.baseline = baseline or UserHealthBaseline(user_id="default")

    def detect(self, metrics: HealthMetrics) -> list[HealthEvent]:
        """检测健康事件"""
        events = []
        now = datetime.now()

        # 1. 压力检测：HRV 持续偏低
        if metrics.hrv_sdnn is not None:
            hrv_ratio = metrics.hrv_sdnn / self.baseline.hrv_sdnn_baseline
            if hrv_ratio < 0.5:
                events.append(HealthEvent(
                    event_type=HealthEventType.STRESS_DETECTED,
                    severity=Severity.HIGH,
                    timestamp=now,
                    description=f"您的心率变异性（HRV）明显偏低，仅为基础值的{hrv_ratio:.0%}，身体可能处于较高压力状态",
                    data_summary={"hrv_sdnn": metrics.hrv_sdnn, "ratio": hrv_ratio},
                ))
            elif hrv_ratio < 0.7:
                events.append(HealthEvent(
                    event_type=HealthEventType.STRESS_DETECTED,
                    severity=Severity.MODERATE,
                    timestamp=now,
                    description=f"您的心率变异性（HRV）偏低，约为基础值的{hrv_ratio:.0%}，可能存在一定压力",
                    data_summary={"hrv_sdnn": metrics.hrv_sdnn, "ratio": hrv_ratio},
                ))

        # 2. 焦虑疑似：静息状态心率异常升高
        if metrics.heart_rate_resting is not None:
            hr_ratio = metrics.heart_rate_resting / self.baseline.resting_hr
            if hr_ratio > 1.4:
                events.append(HealthEvent(
                    event_type=HealthEventType.ANXIETY_SUSPECTED,
                    severity=Severity.HIGH,
                    timestamp=now,
                    description=f"您当前静息心率偏高（{metrics.heart_rate_resting:.0f}次/分），比日常高出{(hr_ratio-1)*100:.0f}%，可能是焦虑或紧张的表现",
                    data_summary={"resting_hr": metrics.heart_rate_resting, "ratio": hr_ratio},
                ))
            elif hr_ratio > 1.2:
                events.append(HealthEvent(
                    event_type=HealthEventType.ANXIETY_SUSPECTED,
                    severity=Severity.MODERATE,
                    timestamp=now,
                    description=f"您的静息心率略高于日常（{metrics.heart_rate_resting:.0f}次/分），请留意自己的情绪状态",
                    data_summary={"resting_hr": metrics.heart_rate_resting, "ratio": hr_ratio},
                ))

        # 3. 睡眠不足
        if metrics.sleep_duration_hours is not None:
            if metrics.sleep_duration_hours < 5:
                events.append(HealthEvent(
                    event_type=HealthEventType.POOR_SLEEP,
                    severity=Severity.HIGH,
                    timestamp=now,
                    description=f"您昨晚睡眠仅{metrics.sleep_duration_hours:.1f}小时，严重不足，建议今晚早点休息",
                    data_summary={"sleep_hours": metrics.sleep_duration_hours},
                ))
            elif metrics.sleep_duration_hours < 6:
                events.append(HealthEvent(
                    event_type=HealthEventType.POOR_SLEEP,
                    severity=Severity.MODERATE,
                    timestamp=now,
                    description=f"您昨晚睡了{metrics.sleep_duration_hours:.1f}小时，略低于推荐时长",
                    data_summary={"sleep_hours": metrics.sleep_duration_hours},
                ))

        # 4. 活动量不足
        if metrics.steps is not None:
            if metrics.steps < self.baseline.daily_steps_avg * 0.3:
                events.append(HealthEvent(
                    event_type=HealthEventType.LOW_ACTIVITY,
                    severity=Severity.LOW,
                    timestamp=now,
                    description=f"您今天的步数较少（{metrics.steps}步），适当活动有助于身心健康",
                    data_summary={"steps": metrics.steps},
                ))

        # 5. 体温异常
        if metrics.temperature_avg is not None:
            low, high = self.baseline.temperature_range
            if metrics.temperature_avg > high:
                events.append(HealthEvent(
                    event_type=HealthEventType.FEVER_SUSPECTED,
                    severity=Severity.MODERATE,
                    timestamp=now,
                    description=f"您的体温偏高（{metrics.temperature_avg:.1f}°C），请注意观察，如有不适请及时就医",
                    data_summary={"temperature": metrics.temperature_avg},
                ))

        return events


def extract_metrics(
    data_type: DataType,
    values: list[HealthDataPoint],
) -> HealthMetrics:
    """从原始数据提取健康指标"""
    if not values:
        return HealthMetrics()

    raw_values = [v.value for v in values]

    metrics = HealthMetrics()

    if data_type == DataType.HEART_RATE:
        metrics.heart_rate_avg = sum(raw_values) / len(raw_values)
        metrics.heart_rate_max = max(raw_values)
        metrics.heart_rate_min = min(raw_values)
        # 取最低的10%作为静息心率的近似
        sorted_vals = sorted(raw_values)
        n = max(1, len(sorted_vals) // 10)
        metrics.heart_rate_resting = sum(sorted_vals[:n]) / n

    elif data_type == DataType.HRV:
        metrics.hrv_sdnn = sum(raw_values) / len(raw_values)
        # RMSSD 如果数据中有，否则与 SDNN 近似
        metrics.hrv_rmssd = metrics.hrv_sdnn * 0.8  # 简化估算

    elif data_type == DataType.TEMPERATURE:
        metrics.temperature_avg = sum(raw_values) / len(raw_values)

    elif data_type == DataType.ACTIVITY:
        metrics.steps = int(sum(raw_values))
        # 假设活跃分钟 = 步数 / 100 的粗略估算
        metrics.active_minutes = int(metrics.steps / 100)

    elif data_type == DataType.SLEEP:
        # 睡眠数据：value 表示该时段的睡眠时长（小时）
        metrics.sleep_duration_hours = sum(raw_values)
        # 睡眠质量评分：简化计算
        target = 7.5
        ratio = min(metrics.sleep_duration_hours / target, 1.2)
        metrics.sleep_quality_score = min(100, ratio * 80)

    return metrics
