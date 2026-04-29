"""Relationship — 关系层级系统

云英与用户的关系会随交互加深而升级，不同层级影响：
- 语气深度：从礼貌关怀到亲密陪伴
- 话题边界：从通用建议到个人化关怀
- 主动程度：从被动回应到主动关心
- 记忆利用：从浅层到深度引用过往对话

层级定义（参考 Character.AI / Replika / 猫箱 的亲密度模型）：

  初识 (0-49分)：礼貌温暖，保持适度距离
  相识 (50-149分)：更自然，开始记住偏好
  知心 (150-349分)：亲密关怀，主动问候，深度引用记忆
  挚友 (350+分)：无话不谈，像多年好友般陪伴

积分规则：
- 每条消息 +1（基础互动）
- 用户倾诉情绪 +3（深度互动）
- 用户主动分享生活 +2（信任信号）
- 连续多日使用（每日首次） +5（坚持信号）
- 用户表达感谢/认可 +2（正向反馈）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from loguru import logger


class RelationLevel(str, Enum):
    """关系层级"""
    ACQUAINTANCE = "初识"   # 0-49
    FAMILIAR = "相识"       # 50-149
    CLOSE = "知心"          # 150-349
    INTIMATE = "挚友"       # 350+


@dataclass
class Relationship:
    """用户与云英的关系状态"""
    user_id: str
    score: int = 0
    level: RelationLevel = RelationLevel.ACQUAINTANCE
    last_active_date: str = ""       # 最后活跃日期 (YYYY-MM-DD)
    total_messages: int = 0          # 累计消息数
    emotion_shares: int = 0          # 情绪倾诉次数
    life_shares: int = 0             # 生活分享次数
    gratitude_count: int = 0         # 感谢/认可次数
    consecutive_days: int = 0        # 连续使用天数
    level_up_at: str = ""            # 上次升级时间


# 积分规则
SCORE_RULES = {
    "message": 1,         # 每条消息
    "emotion_share": 3,   # 情绪倾诉
    "life_share": 2,      # 生活分享
    "daily_login": 5,     # 每日首次使用
    "gratitude": 2,       # 感谢/认可
}


def get_level(score: int) -> RelationLevel:
    """根据积分返回关系层级"""
    if score >= 350:
        return RelationLevel.INTIMATE
    elif score >= 150:
        return RelationLevel.CLOSE
    elif score >= 50:
        return RelationLevel.FAMILIAR
    else:
        return RelationLevel.ACQUAINTANCE


def get_level_prompt_suffix(level: RelationLevel) -> str:
    """根据关系层级返回 prompt 注入文本——调整语气和行为边界

    这个文本会被追加到 system prompt 中，指导模型在不同关系深度下
    采用不同的回复风格
    """
    suffixes = {
        RelationLevel.ACQUAINTANCE: (
            "【关系：初识】你们刚认识，语气温暖但保持适度距离。"
            "多用'你'少用'咱们'，建议通用为主，不过度深入私人话题。"
            "称呼用'你'。"
        ),
        RelationLevel.FAMILIAR: (
            "【关系：相识】你们已经聊过几次，语气更自然轻松。"
            "可以开始记住用户提到的偏好和习惯，偶尔自然地引用。"
            "偶尔用'咱们'，建议可以更个人化。称呼用'你'。"
        ),
        RelationLevel.CLOSE: (
            "【关系：知心】你们已经是知心朋友，语气亲密关怀。"
            "主动问候用户之前提到的事（如'上次失眠好点了吗'），"
            "可以更直接地表达关心，用'咱们'更多，称呼可以用昵称。"
            "建议更具体更个人化。"
        ),
        RelationLevel.INTIMATE: (
            "【关系：挚友】你们是无话不谈的挚友，语气如多年好友般自然。"
            "像对老朋友一样说话，可以直接表达关心和担心，"
            "主动提及过往对话中的细节，用'咱们'很自然。"
            "建议可以更直接、更贴心。称呼可以用亲昵的方式。"
        ),
    }
    return suffixes.get(level, suffixes[RelationLevel.ACQUAINTANCE])


# 情绪倾诉关键词（用于判断是否是情绪分享）
_EMOTION_SHARE_KEYWORDS = [
    "焦虑", "压力", "烦", "累", "难过", "伤心", "抑郁", "害怕",
    "孤独", "不开心", "心烦", "委屈", "想哭", "哭", "紧张",
    "暴躁", "低落", "郁闷", "喘不过气", "睡不着", "半夜醒",
    "我最近", "感觉", "好烦", "受不了", "撑不住",
]

# 生活分享关键词
_LIFE_SHARE_KEYWORDS = [
    "今天", "昨天", "周末", "放假", "去了", "吃了", "买了",
    "见了", "做了", "开始", "终于", "打算", "准备",
]

# 感谢/认可关键词
_GRATITUDE_KEYWORDS = [
    "谢谢", "感谢", "多亏", "托你", "有你真好", "你真好",
    "你真好", "帮了大忙", "真好", "好温暖", "被安慰到",
    "舒服多了", "好多了", "心情好多了",
]


def classify_message(user_message: str) -> list[str]:
    """判断用户消息的类型，用于积分计算

    Returns:
        命中的积分类型列表，如 ["message", "emotion_share"]
    """
    msg = user_message.lower()
    hits = ["message"]  # 基础分

    if any(kw in msg for kw in _EMOTION_SHARE_KEYWORDS):
        hits.append("emotion_share")

    if any(kw in msg for kw in _LIFE_SHARE_KEYWORDS):
        hits.append("life_share")

    if any(kw in msg for kw in _GRATITUDE_KEYWORDS):
        hits.append("gratitude")

    return hits


def calculate_score(hits: list[str]) -> int:
    """根据命中的类型计算本轮积分"""
    total = 0
    for hit in hits:
        total += SCORE_RULES.get(hit, 0)
    return total


def check_daily_login(relationship: Relationship) -> bool:
    """检查是否是今日首次使用（每日登录奖励）

    Returns:
        True 表示今日首次（应加分），False 表示今日已活跃过
    """
    today = datetime.now().strftime("%Y-%m-%d")

    if relationship.last_active_date != today:
        # 计算连续天数
        if relationship.last_active_date:
            try:
                last = datetime.strptime(relationship.last_active_date, "%Y-%m-%d")
                now = datetime.strptime(today, "%Y-%m-%d")
                diff = (now - last).days
                if diff == 1:
                    relationship.consecutive_days += 1
                elif diff > 1:
                    relationship.consecutive_days = 1
            except ValueError:
                relationship.consecutive_days = 1
        else:
            relationship.consecutive_days = 1

        relationship.last_active_date = today
        return True

    return False


def update_relationship(relationship: Relationship, user_message: str) -> tuple[Relationship, bool]:
    """更新关系状态

    Args:
        relationship: 当前关系状态
        user_message: 用户消息

    Returns:
        (更新后的关系, 是否刚升级)
    """
    # 1. 消息类型分类 + 积分
    hits = classify_message(user_message)
    score_gain = calculate_score(hits)

    # 2. 每日登录奖励
    if check_daily_login(relationship):
        score_gain += SCORE_RULES["daily_login"]

    # 3. 更新计数
    relationship.total_messages += 1
    if "emotion_share" in hits:
        relationship.emotion_shares += 1
    if "life_share" in hits:
        relationship.life_shares += 1
    if "gratitude" in hits:
        relationship.gratitude_count += 1

    # 4. 更新积分和层级
    old_level = relationship.level
    relationship.score += score_gain
    relationship.level = get_level(relationship.score)

    just_leveled_up = relationship.level != old_level
    if just_leveled_up:
        relationship.level_up_at = datetime.now().isoformat()
        logger.info(
            "用户 {} 关系升级: {} → {} (积分 {})",
            relationship.user_id, old_level.value, relationship.level.value, relationship.score,
        )

    return relationship, just_leveled_up
