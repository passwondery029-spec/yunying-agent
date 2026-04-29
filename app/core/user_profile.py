"""用户画像自动完善 — 从对话中提取用户特征，越用越懂你

核心思路：
1. LLM 从用户消息中提取结构化特征（不增加额外模型调用，复用回复生成）
2. JSON 存储画像（不需要图数据库）
3. 注入 prompt 让 AI 自然运用画像信息

画像维度：
- 基础属性: 年龄段/性别/职业/作息/运动习惯
- 健康关注: 主要困扰/体质倾向/过往病史
- 情绪模式: 常见情绪/压力源/应对方式
- 偏好: 冥想偏好/沟通风格/活动偏好
- 生活: 饮食习惯/社交状态/居住环境
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from loguru import logger


# === 画像维度定义 ===

PROFILE_DIMENSIONS = {
    "age_range": {
        "label": "年龄段",
        "type": "str",
        "examples": ["20-25", "25-30", "30-35", "35-40", "40+"],
        "extract_hint": "从用户提到的工作年限、生活阶段等推断",
    },
    "occupation": {
        "label": "职业",
        "type": "str",
        "examples": ["互联网/IT", "教育", "医疗", "自由职业", "学生", "金融", "公务员", "未知"],
        "extract_hint": "从加班、通勤、工作内容等推断",
    },
    "schedule": {
        "label": "作息",
        "type": "str",
        "examples": ["早睡早起", "晚睡晚起", "经常加班晚睡", "作息不规律", "未知"],
        "extract_hint": "从提到的睡觉时间、起床时间推断",
    },
    "exercise_habit": {
        "label": "运动习惯",
        "type": "str",
        "examples": ["基本不运动", "偶尔散步", "每周运动2-3次", "每天运动", "未知"],
        "extract_hint": "从步数、提到的运动推断",
    },
    "main_concerns": {
        "label": "主要困扰",
        "type": "list[str]",
        "examples": [["失眠", "焦虑"], ["压力", "疲劳"], ["情绪低落"]],
        "extract_hint": "用户反复提到的问题",
    },
    "constitution_tendency": {
        "label": "体质倾向",
        "type": "str",
        "examples": ["气虚", "阳虚", "阴虚", "痰湿", "气郁", "平和", "未知"],
        "extract_hint": "从怕冷/怕热/易疲劳/易出汗等推断",
    },
    "common_emotions": {
        "label": "常见情绪",
        "type": "list[str]",
        "examples": [["焦虑", "紧张"], ["低落", "孤独"], ["烦躁", "易怒"]],
        "extract_hint": "用户最常表达的情绪",
    },
    "stress_sources": {
        "label": "压力源",
        "type": "list[str]",
        "examples": [["工作"], ["人际关系"], ["经济"], ["家庭"]],
        "extract_hint": "导致压力的具体原因",
    },
    "coping_style": {
        "label": "应对方式",
        "type": "str",
        "examples": ["内化压抑", "倾诉释放", "逃避转移", "主动解决", "未知"],
        "extract_hint": "面对压力时习惯怎么做",
    },
    "meditation_preference": {
        "label": "冥想偏好",
        "type": "str",
        "examples": ["呼吸冥想", "身体扫描", "音乐冥想", "未尝试过", "抗拒"],
        "extract_hint": "对冥想的态度和偏好",
    },
    "communication_style": {
        "label": "沟通风格",
        "type": "str",
        "examples": ["简短直接", "喜欢详细描述", "情绪化表达", "理性分析", "未知"],
        "extract_hint": "用户的说话方式",
    },
    "diet_habit": {
        "label": "饮食习惯",
        "type": "str",
        "examples": ["三餐规律", "经常外卖", "爱吃辛辣", "饮食清淡", "不规律", "未知"],
        "extract_hint": "从提到的饮食相关内容推断",
    },
    "social_status": {
        "label": "社交状态",
        "type": "str",
        "examples": ["独居", "合租", "与家人住", "与伴侣住", "未知"],
        "extract_hint": "从提到的居住情况推断",
    },
    "pet_info": {
        "label": "宠物",
        "type": "str",
        "examples": ["养猫", "养狗", "无宠物", "未知"],
        "extract_hint": "是否提到宠物",
    },
}


# === 提取 Prompt ===

EXTRACT_PROFILE_PROMPT = """你是一个用户画像提取器。从用户的对话消息中，提取结构化的用户特征。

【用户画像当前状态】
{current_profile}

【最近对话】
{recent_messages}

【提取规则】
1. 只提取有明确依据的特征，没有依据的保持原值不变
2. 对于 list 类型，增量合并（不删除已有的，只补充新的）
3. 对于 str 类型，有新证据时覆盖旧值
4. 输出 JSON，只包含有变化的字段，未变化的不要输出
5. 不要编造信息

【可提取维度】
{dimensions_desc}

【输出格式】
仅输出 JSON，不要任何解释文字。例如：
{{"main_concerns": ["失眠", "焦虑"], "schedule": "经常加班晚睡", "occupation": "互联网/IT"}}
如果没有任何特征可以提取，输出：{{}}"""


def build_dimensions_desc() -> str:
    """构建维度描述文本"""
    lines = []
    for key, dim in PROFILE_DIMENSIONS.items():
        examples = "、".join(str(e) for e in dim["examples"][:5])
        lines.append(f"- {key}（{dim['label']}）: {dim['extract_hint']}。可能值如: {examples}")
    return "\n".join(lines)


async def extract_profile_updates(
    llm_client,
    current_profile: dict[str, Any],
    recent_messages: list[dict[str, str]],
) -> dict[str, Any]:
    """从最近对话中提取用户画像更新

    Args:
        llm_client: LLM 客户端
        current_profile: 当前画像（dict 形式）
        recent_messages: 最近对话 [{"role": "user"/"assistant", "content": "..."}]

    Returns:
        需要更新的字段 dict，如 {"occupation": "互联网/IT", "main_concerns": ["失眠"]}
    """
    # 格式化当前画像
    profile_lines = []
    for key, dim in PROFILE_DIMENSIONS.items():
        value = current_profile.get(key, "未知" if dim["type"] == "str" else [])
        profile_lines.append(f"- {dim['label']}（{key}）: {value}")
    current_profile_text = "\n".join(profile_lines)

    # 格式化最近对话（只取用户消息，最多最近 10 条）
    user_msgs = [m for m in recent_messages if m.get("role") == "user"][-10:]
    if not user_msgs:
        return {}

    messages_text = "\n".join(f"用户: {m['content']}" for m in user_msgs)

    # 构建提取 prompt
    prompt = EXTRACT_PROFILE_PROMPT.format(
        current_profile=current_profile_text,
        recent_messages=messages_text,
        dimensions_desc=build_dimensions_desc(),
    )

    try:
        result = await llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,  # 低温度，确保稳定输出
            max_tokens=300,
        )

        # 解析 JSON
        text = result.strip()
        # 尝试提取 JSON 块
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        updates = json.loads(text)

        if not isinstance(updates, dict) or not updates:
            return {}

        # 校验字段合法性
        valid_updates = {}
        for key, value in updates.items():
            if key in PROFILE_DIMENSIONS:
                valid_updates[key] = value

        if valid_updates:
            logger.info(f"📊 画像提取更新: {valid_updates}")

        return valid_updates

    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"画像提取失败: {e}")
        return {}


def profile_to_prompt_text(profile: dict[str, Any]) -> str:
    """将用户画像转为 prompt 注入文本

    Args:
        profile: 用户画像 dict

    Returns:
        注入 prompt 的自然语言文本
    """
    parts = []

    for key, dim in PROFILE_DIMENSIONS.items():
        value = profile.get(key)
        if not value or value == "未知" or value == []:
            continue

        if isinstance(value, list):
            value_str = "、".join(value)
            parts.append(f"{dim['label']}: {value_str}")
        else:
            parts.append(f"{dim['label']}: {value}")

    if not parts:
        return ""

    return "【用户画像（根据历史对话自动整理，你可以在回复中自然运用这些信息）】\n" + "\n".join(f"- {p}" for p in parts)


def profile_to_dict(profile_obj) -> dict[str, Any]:
    """将 UserProfile 对象转为 dict（用于提取时传入）"""
    # 从 store.py 的 UserProfile 提取关键字段
    result = {}
    for key in PROFILE_DIMENSIONS:
        if hasattr(profile_obj, key):
            val = getattr(profile_obj, key)
            if val is not None:
                result[key] = val

    # 从已有字段映射
    if hasattr(profile_obj, "constitution") and profile_obj.constitution != "未测评":
        result.setdefault("constitution_tendency", profile_obj.constitution)
    if hasattr(profile_obj, "main_concerns") and profile_obj.main_concerns:
        result.setdefault("main_concerns", profile_obj.main_concerns)
    if hasattr(profile_obj, "emotion_trend") and profile_obj.emotion_trend != "暂无数据":
        result.setdefault("common_emotions", [profile_obj.emotion_trend])

    return result


def apply_updates_to_profile(profile_obj, updates: dict[str, Any]):
    """将提取的更新应用到 UserProfile 对象"""
    for key, value in updates.items():
        if hasattr(profile_obj, key):
            # list 类型：增量合并
            current = getattr(profile_obj, key)
            if isinstance(current, list) and isinstance(value, list):
                merged = list(set(current + value))  # 去重
                setattr(profile_obj, key, merged)
            else:
                setattr(profile_obj, key, value)

    # 反向映射到已有字段
    if "constitution_tendency" in updates and hasattr(profile_obj, "constitution"):
        profile_obj.constitution = updates["constitution_tendency"]
    if "main_concerns" in updates and hasattr(profile_obj, "main_concerns"):
        existing = profile_obj.main_concerns or []
        new = updates["main_concerns"]
        profile_obj.main_concerns = list(set(existing + new))
