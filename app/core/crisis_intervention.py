"""
危机干预模块 — 法规第十三条合规

功能：
1. 检测用户极端情绪（自残、自杀、严重危机）
2. 注入危机干预提示到 prompt
3. 回复后置检查：如果 AI 没有关怀性回应则补充
4. 记录危机事件
"""

import re
import json
import os
from datetime import datetime
from typing import Optional, Tuple


# 危机关键词（高优先级 - 自残自杀相关）
HIGH_RISK_KEYWORDS = [
    "不想活了", "想死", "自杀", "跳楼", "割腕", "吃药死", "活不下去",
    "了结自己", "结束生命", "想离开这个世界", "活着没意思", "不想再活",
    "解脱", "一了百了", "去死", "写遗书", "永别",
    "自我伤害", "伤害自己", "烧炭",
]

# 危机关键词（中优先级 - 严重情绪危机）
MEDIUM_RISK_KEYWORDS = [
    "崩溃", "绝望", "撑不下去", "看不到希望", "走投无路",
    "世界不需要我", "没人会在乎", "没有出路", "无法承受",
    "彻底失败", "无法继续", "再也好不了",
]

# 心理援助热线（法规要求提供）
CRISIS_HOTLINES = {
    "全国24小时心理援助热线": "400-161-9995",
    "北京心理危机研究与干预中心": "010-82951332",
    "希望24热线": "400-161-9995",
    "生命热线": "400-821-1215",
}


def detect_crisis_level(user_message: str) -> Tuple[str, int]:
    """
    检测用户消息中的危机等级
    
    Returns:
        (level, score): level = "none"/"medium"/"high", score = 0/1/2
    """
    msg = user_message.lower()
    
    # 高危检测
    for kw in HIGH_RISK_KEYWORDS:
        if kw in msg:
            return "high", 2
    
    # 中危检测
    for kw in MEDIUM_RISK_KEYWORDS:
        if kw in msg:
            return "medium", 1
    
    return "none", 0


def get_crisis_prompt_injection(level: str) -> Optional[str]:
    """
    根据危机等级，返回需要注入到 prompt 的危机干预指令
    """
    if level == "none":
        return None
    
    if level == "high":
        return (
            "\n\n⚠️【紧急危机干预指令】\n"
            "用户正在表达严重的自我伤害或自杀倾向。你必须：\n"
            "1. 立即表达深切关怀，绝不冷漠或说教\n"
            "2. 明确告诉用户：你不是一个人，有人愿意倾听和帮助\n"
            "3. 必须提供心理援助热线：400-161-9995（24小时）\n"
            "4. 鼓励用户联系身边的人或专业帮助\n"
            "5. 绝对不要说'你想开点''别想太多'等轻描淡写的话\n"
            "6. 不要主动结束对话，保持陪伴\n"
            "7. 用温暖但不过度煽情的语气\n"
        )
    
    if level == "medium":
        return (
            "\n\n⚠️【情绪关怀指令】\n"
            "用户正在经历严重的情绪痛苦。你需要：\n"
            "1. 先充分认可和接纳用户的感受，不要急于建议\n"
            "2. 用温暖的语言让用户感到被理解\n"
            "3. 如果用户需要，提供心理援助热线：400-161-9995\n"
            "4. 引导用户关注当下能做的一件小事\n"
            "5. 表达持续陪伴的态度\n"
        )
    
    return None


def check_crisis_response(reply: str, level: str) -> Tuple[str, bool]:
    """
    检查 AI 回复是否包含必要的危机干预内容
    
    Returns:
        (modified_reply, was_modified)
    """
    if level == "none":
        return reply, False
    
    # 高危：检查是否提供了热线
    has_hotline = any(phone in reply for phone in ["400-161-9995", "400-821-1215", "010-82951332"])
    has_empathy = any(kw in reply for kw in ["你不是一个人", "有人愿意", "陪着你", "在乎你", "在这里"])
    
    if level == "high" and (not has_hotline or not has_empathy):
        crisis_suffix = (
            "\n\n🆘 如果你正在经历非常痛苦的时刻，请记住你不是一个人。\n"
            "📞 24小时心理援助热线：**400-161-9995**\n"
            "请拨打这个电话，会有人倾听你、帮助你。"
        )
        return reply + crisis_suffix, True
    
    if level == "medium" and not has_empathy:
        empathy_suffix = (
            "\n\n不管现在有多难，你不是一个人在面对。如果你需要，随时可以拨打 400-161-9995 找人聊聊。"
        )
        return reply + empathy_suffix, True
    
    return reply, False


def log_crisis_event(user_id: str, user_message: str, level: str, ai_reply: str):
    """
    记录危机事件到日志文件（法规要求留存记录）
    """
    log_dir = "data"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "crisis_events.jsonl")
    
    event = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
        "level": level,
        "user_message_snippet": user_message[:100],
        "ai_reply_snippet": ai_reply[:200],
        "hotline_provided": "400-161-9995" in ai_reply,
    }
    
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
