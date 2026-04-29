"""Health Trend — 健康趋势追踪

追踪用户健康指标的变化趋势，用于：
1. 构建趋势快照注入 prompt，让 AI 知道"变好/变差/稳定"
2. 触发主动关怀（如心率持续偏高、睡眠持续下降时提醒关注）
3. 发现异常模式（如 HRV 突然大幅下降）

趋势判断规则：
- 只需要最近 2-3 次数据点即可判断趋势
- 变化超过阈值才算"变化"，否则视为"稳定"
- 趋势文本直接注入 prompt，让 AI 自然地提及
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from loguru import logger


class TrendDirection(str, Enum):
    """趋势方向"""
    IMPROVING = "改善"     # 指标向好
    STABLE = "稳定"        # 无明显变化
    DECLINING = "下降"     # 指标变差
    NO_DATA = "暂无数据"


@dataclass
class MetricTrend:
    """单个指标的趋势"""
    name: str              # 指标名称
    direction: TrendDirection
    detail: str            # 趋势描述文本（注入 prompt）
    alert: bool = False    # 是否需要主动关怀提醒


# 趋势判断阈值
_THRESHOLDS = {
    "heart_rate": {"improve": -3, "decline": 3},     # 心率下降=改善，上升=下降
    "hrv": {"improve": 3, "decline": -3},             # HRV上升=改善，下降=下降
    "sleep_hours": {"improve": 0.5, "decline": -0.5}, # 睡眠增加=改善，减少=下降
    "steps": {"improve": 1000, "decline": -1000},     # 步数增加=改善
    "body_temp": {"improve": 0.0, "decline": 0.5},    # 体温异常升高=下降
}

# 指标中文名
_METRIC_NAMES = {
    "heart_rate": "心率",
    "hrv": "心率变异性(HRV)",
    "sleep_hours": "睡眠时长",
    "steps": "步数",
    "body_temp": "体温",
}

# 异常阈值（超过此值触发 alert）
_ALERT_THRESHOLDS = {
    "heart_rate_high": 100,     # 静息心率>100
    "heart_rate_low": 50,       # 静息心率<50
    "hrv_low": 30,              # HRV<30
    "sleep_low": 5.0,           # 睡眠<5小时
    "body_temp_high": 37.3,     # 体温>37.3
    "steps_low": 2000,          # 步数<2000
}


def _extract_metrics(data: dict) -> dict[str, float | None]:
    """从健康数据 dict 中提取各指标值"""
    if not data:
        return {}

    return {
        "heart_rate": data.get("heart_rate"),
        "hrv": data.get("hrv"),
        "sleep_hours": data.get("sleep_hours") or data.get("sleep_quality"),
        "steps": data.get("steps"),
        "body_temp": data.get("body_temp"),
    }


def analyze_trend(current: dict | None, previous: dict | None = None) -> list[MetricTrend]:
    """分析健康趋势

    Args:
        current: 当前健康指标
        previous: 上一次健康指标（可为 None，只有一条数据时判断绝对值）

    Returns:
        各指标的趋势列表
    """
    cur = _extract_metrics(current)
    prev = _extract_metrics(previous)

    trends = []

    for metric_key, threshold in _THRESHOLDS.items():
        cur_val = cur.get(metric_key)
        if cur_val is None:
            continue

        metric_name = _METRIC_NAMES.get(metric_key, metric_key)

        # 检查是否异常（触发 alert）
        alert = False
        alert_reason = ""

        if metric_key == "heart_rate":
            if cur_val > _ALERT_THRESHOLDS["heart_rate_high"]:
                alert = True
                alert_reason = f"静息心率{cur_val:.0f}bpm偏高"
            elif cur_val < _ALERT_THRESHOLDS["heart_rate_low"]:
                alert = True
                alert_reason = f"静息心率{cur_val:.0f}bpm偏低"
        elif metric_key == "hrv":
            if cur_val < _ALERT_THRESHOLDS["hrv_low"]:
                alert = True
                alert_reason = f"HRV仅{cur_val:.0f}ms，自主神经调节偏弱"
        elif metric_key == "sleep_hours":
            if cur_val < _ALERT_THRESHOLDS["sleep_low"]:
                alert = True
                alert_reason = f"睡眠仅{cur_val:.1f}小时，严重不足"
        elif metric_key == "steps":
            if cur_val < _ALERT_THRESHOLDS["steps_low"]:
                alert = True
                alert_reason = f"步数仅{cur_val:.0f}步，活动量很低"
        elif metric_key == "body_temp":
            if cur_val > _ALERT_THRESHOLDS["body_temp_high"]:
                alert = True
                alert_reason = f"体温{cur_val:.1f}°C偏高"

        # 判断趋势
        prev_val = prev.get(metric_key)

        if prev_val is not None:
            diff = cur_val - prev_val

            if metric_key == "heart_rate" or metric_key == "body_temp":
                # 心率和体温：下降=改善
                if diff <= threshold["improve"]:
                    direction = TrendDirection.IMPROVING
                    detail = f"{metric_name}从{prev_val:.0f}→{cur_val:.0f}，有所改善"
                elif diff >= threshold["decline"]:
                    direction = TrendDirection.DECLINING
                    detail = f"{metric_name}从{prev_val:.0f}→{cur_val:.0f}，有所上升（需关注）"
                else:
                    direction = TrendDirection.STABLE
                    detail = f"{metric_name}{cur_val:.0f}，保持稳定"
            else:
                # HRV/睡眠/步数：上升=改善
                if diff >= threshold["improve"]:
                    direction = TrendDirection.IMPROVING
                    detail = f"{metric_name}从{prev_val:.0f}→{cur_val:.0f}，有所提升"
                elif diff <= threshold["decline"]:
                    direction = TrendDirection.DECLINING
                    detail = f"{metric_name}从{prev_val:.0f}→{cur_val:.0f}，有所下降（需关注）"
                else:
                    direction = TrendDirection.STABLE
                    detail = f"{metric_name}{cur_val:.0f}，保持稳定"
        else:
            # 没有历史数据，只判断绝对值
            direction = TrendDirection.STABLE
            detail = f"{metric_name}{cur_val:.0f}"
            if alert:
                detail += f"（{alert_reason}）"

        if alert and not alert_reason in detail:
            detail += f" ⚠️{alert_reason}"

        trends.append(MetricTrend(
            name=metric_name,
            direction=direction,
            detail=detail,
            alert=alert,
        ))

    return trends


def build_trend_prompt(trends: list[MetricTrend]) -> str:
    """构建趋势快照文本，注入到 prompt 中

    Args:
        trends: 各指标趋势

    Returns:
        格式化的趋势文本
    """
    if not trends:
        return ""

    lines = ["## 健康趋势（基于最近的数据变化）"]

    for t in trends:
        icon = {"改善": "📈", "稳定": "➡️", "下降": "📉", "暂无数据": "❓"}.get(t.direction.value, "➡️")
        lines.append(f"- {icon} {t.detail}")

    # 检查是否有需要主动关怀的 alert
    alerts = [t for t in trends if t.alert]
    if alerts:
        lines.append("\n⚠️ 注意：以下指标需要特别关注，请自然地提醒用户：")
        for a in alerts:
            lines.append(f"  - {a.detail}")

    return "\n".join(lines)


def get_trend_summary(trends: list[MetricTrend]) -> str:
    """获取简短的趋势总结（用于情感节点/记忆）

    Returns:
        一句话趋势总结
    """
    if not trends:
        return "暂无健康趋势数据"

    declining = [t for t in trends if t.direction == TrendDirection.DECLINING]
    improving = [t for t in trends if t.direction == TrendDirection.IMPROVING]
    alerting = [t for t in trends if t.alert]

    if alerting:
        return f"健康预警：{', '.join(a.name for a in alerting)}"
    elif declining:
        return f"部分指标下降：{', '.join(d.name for d in declining)}"
    elif improving:
        return f"整体趋势向好：{', '.join(i.name for i in improving)}"
    else:
        return "各项指标稳定"
