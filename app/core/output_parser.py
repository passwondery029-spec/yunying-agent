"""结构化输出解析器

从 LLM 返回的文本中提取特殊标记，解析为结构化的 ContentBlock。
同时保留纯文本内容作为 reply 字段（向后兼容）。

标记格式：
  <<type>> ... <</type>>

支持的类型：health_tip, action, acupoint, meditation, product
"""

import re
from app.api.schemas import (
    TextBlock, ActionBlock, MeditationBlock,
    ProductBlock, HealthTipBlock, ContentBlock,
)


# 提取 <<type>>...<</type>> 块的正则
_BLOCK_PATTERN = re.compile(
    r'<<(\w+)>>\s*\n(.*?)\n\s*<</\1>>',
    re.DOTALL,
)


def parse_blocks(text: str) -> tuple[str, list[ContentBlock]]:
    """解析 LLM 返回文本中的结构化标记

    Args:
        text: LLM 原始返回文本

    Returns:
        (纯文本reply, 结构化内容块列表)
    """
    blocks: list[ContentBlock] = []
    clean_text = text

    # 找到所有标记块
    matches = list(_BLOCK_PATTERN.finditer(text))

    for match in matches:
        block_type = match.group(1)
        block_content = match.group(2).strip()

        # 从原文中移除标记块（保留周围的自然文字）
        clean_text = clean_text.replace(match.group(0), "")

        # 解析各类型
        parsed = _parse_single_block(block_type, block_content)
        if parsed:
            blocks.append(parsed)

    # 清理纯文本（去掉多余空行）
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text.strip())

    return clean_text, blocks


def _parse_single_block(block_type: str, content: str) -> ContentBlock | None:
    """解析单个标记块"""
    fields = _parse_key_value_fields(content)

    if block_type == "health_tip":
        return HealthTipBlock(
            title=fields.get("标题", "健康提示"),
            content=fields.get("内容", content),
            severity=fields.get("严重度", "info"),
        )

    elif block_type == "action":
        return ActionBlock(
            label=fields.get("按钮文字", "了解更多"),
            action=fields.get("动作", "learn_more"),
            params=_parse_params(fields.get("参数", "")),
        )

    elif block_type == "acupoint":
        # 穴位按摩转为健康提示卡
        return HealthTipBlock(
            title=f"穴位按摩：{fields.get('穴位名', '')}",
            content=f"位置：{fields.get('位置', '')}\n按法：{fields.get('按法', '')}\n时长：{fields.get('时长', '')}",
            severity="info",
        )

    elif block_type == "meditation":
        # 提取步骤（步骤1、步骤2...）
        steps = []
        for i in range(1, 10):
            step = fields.get(f"步骤{i}")
            if step:
                steps.append(step)
            else:
                break
        if not steps:
            # 兜底：把内容按行切分
            steps = [line.strip() for line in content.split("\n") if line.strip() and not line.strip().startswith(("标题", "类型", "时长", "步骤"))]

        return MeditationBlock(
            title=fields.get("标题", "冥想引导"),
            steps=steps,
            duration_minutes=_safe_int(fields.get("时长", "5"), 5),
            style=fields.get("类型", "breathing"),
        )

    elif block_type == "product":
        return ProductBlock(
            name=fields.get("名称", ""),
            description=fields.get("描述", ""),
            price=fields.get("价格"),
            tcm_rationale=fields.get("中医推荐理由"),
        )

    return None


def _parse_key_value_fields(content: str) -> dict[str, str]:
    """解析 key：value 格式的字段"""
    fields = {}
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 支持中文冒号和英文冒号
        for sep in ["：", ":"]:
            if sep in line:
                key, value = line.split(sep, 1)
                fields[key.strip()] = value.strip()
                break
    return fields


def _parse_params(params_str: str) -> dict:
    """解析参数字符串 key=value"""
    params = {}
    if not params_str:
        return params
    for pair in params_str.split(","):
        pair = pair.strip()
        if "=" in pair:
            key, value = pair.split("=", 1)
            params[key.strip()] = value.strip()
    return params


def _safe_int(value: str, default: int = 0) -> int:
    """安全解析整数，提取数字部分"""
    import re
    nums = re.findall(r'\d+', str(value))
    return int(nums[0]) if nums else default
