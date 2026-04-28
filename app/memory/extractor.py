"""LLM 驱动的记忆碎片提取器

从对话历史中提取值得长期记住的信息碎片。
触发时机：每5轮对话 或 会话结束时。
异步执行，不阻塞对话。
"""

import json
import re
from datetime import datetime

from loguru import logger

from app.config import get_settings
from app.core.llm import chat
from app.memory.fragments import (
    CATEGORIES,
    CATEGORY_IMPORTANCE,
    MemoryFragment,
    fragment_store,
)


EXTRACTION_PROMPT = """从对话中提取值得长期记住的信息碎片。

规则：
1. 只提取事实性/模式性/偏好性信息，不提取闲聊
2. 每条1-3句话，自包含（脱离原对话也能理解）
3. 如新碎片与已有碎片矛盾，在invalidates字段填被推翻的碎片ID
4. importance：自伤=1.0，体质/病史/用药=0.9，疗愈进展=0.8，情绪模式=0.7，产品意向=0.6，生活偏好=0.5
5. 最多3条碎片（宁缺毋滥）

类别：health_fact(体质症状) | emotion_pattern(情绪模式) | life_preference(偏好) | relationship(人际) | healing_progress(疗效) | product_intent(购买意向)

已有记忆：
{existing_memories}

对话片段：
{conversation}

输出JSON数组（不要其他内容）：
```json
[{{"content":"碎片内容","category":"类别","tags":["标签"],"importance":0.8,"emotion":"焦虑或无","constitution":"阳虚等或无","invalidates":"被推翻ID或null"}}]
```
无值得提取则输出 `[]`"""


def _format_conversation(messages: list[dict]) -> str:
    """格式化对话历史为可读文本"""
    lines = []
    for msg in messages:
        role = "用户" if msg.get("role") == "user" else "云英"
        content = msg.get("content", "")
        # 截断过长消息
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _format_existing_memories(fragments: list[MemoryFragment]) -> str:
    """格式化已有记忆碎片"""
    if not fragments:
        return "（暂无）"
    lines = []
    for frag in fragments:
        lines.append(f"[{frag.id}] [{frag.category}] {frag.content} (tags: {', '.join(frag.tags)})")
    return "\n".join(lines)


async def extract_fragments(
    user_id: str,
    session_messages: list[dict],
    source_session: str = "",
) -> list[MemoryFragment]:
    """从对话中提取记忆碎片

    Args:
        user_id: 用户ID
        session_messages: 对话历史 [{"role": "user/assistant", "content": "..."}]
        source_session: 来源会话ID

    Returns:
        新提取的记忆碎片列表
    """
    if not session_messages or len(session_messages) < 2:
        return []

    # 只取最近6轮消息（降低延迟和token消耗）
    recent_messages = session_messages[-12:] if len(session_messages) > 12 else session_messages

    # 获取已有碎片（用于去重和冲突检测）
    mem = fragment_store.load_user_memory(user_id)
    existing = [f for f in mem.fragments if f.is_valid]
    existing_text = _format_existing_memories(existing)

    # 格式化对话
    conv_text = _format_conversation(recent_messages)

    # 构建提取 prompt
    prompt = EXTRACTION_PROMPT.format(
        existing_memories=existing_text,
        conversation=conv_text,
    )

    # 确定提取模型：优先用轻量模型（更快更省），无配置则用主模型
    settings = get_settings()
    extractor_model = settings.llm_extractor_model or None  # None = 走降级链

    try:
        # 调用 LLM
        response = await chat(
            messages=[
                {"role": "system", "content": "你是记忆提取器。只输出JSON数组。"},
                {"role": "user", "content": prompt},
            ],
            model=extractor_model,
            temperature=0.2,
            max_tokens=600,
        )

        if not response:
            logger.debug(f"用户 {user_id}: 提取碎片时LLM无响应")
            return []

        # 解析 LLM 输出
        fragments = _parse_extraction_response(
            response, user_id, source_session
        )

        if fragments:
            # 写入存储
            fragment_store.add_fragments(user_id, fragments)
            logger.info(
                f"用户 {user_id}: 提取到 {len(fragments)} 条记忆碎片"
            )

            # P1-8: 体质自动识别 — 碎片中有constitution字段时自动更新画像
            await _update_constitution_from_fragments(user_id, fragments)

        return fragments

    except Exception as e:
        logger.error(f"用户 {user_id}: 记忆碎片提取失败: {e}")
        return []


def _parse_extraction_response(
    response: str,
    user_id: str,
    source_session: str,
) -> list[MemoryFragment]:
    """解析 LLM 提取结果"""
    # 尝试提取 JSON 数组
    json_match = re.search(r'\[[\s\S]*\]', response)
    if not json_match:
        logger.debug(f"提取碎片: LLM输出中未找到JSON数组: {response[:200]}")
        return []

    try:
        items = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        logger.warning(f"提取碎片: JSON解析失败: {e}")
        return []

    if not isinstance(items, list):
        return []

    fragments = []
    for item in items[:3]:  # 强制最多3条
        if not isinstance(item, dict):
            continue

        content = item.get("content", "").strip()
        if not content or len(content) < 5:
            continue

        category = item.get("category", "health_fact")
        if category not in CATEGORIES:
            category = "health_fact"

        importance = item.get("importance", CATEGORY_IMPORTANCE.get(category, 0.5))
        try:
            importance = float(importance)
            importance = max(0.0, min(1.0, importance))
        except (ValueError, TypeError):
            importance = CATEGORY_IMPORTANCE.get(category, 0.5)

        tags = item.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        tags = [str(t) for t in tags[:4]]  # 最多4个标签

        frag = MemoryFragment(
            user_id=user_id,
            content=content,
            category=category,
            source_session=source_session,
            source_time=datetime.now(),
            source_summary=content[:50],
            tags=tags,
            constitution=item.get("constitution") if item.get("constitution") not in ("无", "未测评", "none", "null", "", None) else None,
            emotion=item.get("emotion") if item.get("emotion") not in ("无", "未测评", "none", "null", "", None) else None,
            importance=importance,
        )

        # 处理推翻关系
        invalidates_id = item.get("invalidates")
        if invalidates_id and isinstance(invalidates_id, str):
            # 在已有碎片中找到被推翻的
            mem = fragment_store.load_user_memory(user_id)
            for old_frag in mem.fragments:
                if old_frag.id == invalidates_id and old_frag.is_valid:
                    old_frag.is_valid = False
                    old_frag.invalidated_by = frag.id

        fragments.append(frag)

    return fragments


# ── 同步版（用于测试） ────────────────────────────────────

def extract_fragments_sync(
    user_id: str,
    session_messages: list[dict],
    source_session: str = "",
) -> list[MemoryFragment]:
    """同步版提取（用于测试）"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 如果已在异步上下文中，创建新线程
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    extract_fragments(user_id, session_messages, source_session),
                )
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(
                extract_fragments(user_id, session_messages, source_session)
            )
    except RuntimeError:
        return asyncio.run(
            extract_fragments(user_id, session_messages, source_session)
        )


# ===== P1-8: 体质自动识别 =====

# 九种体质的关键症状映射
CONSTITUTION_PATTERNS = {
    "阳虚": {
        "keywords": ["怕冷", "手脚冰凉", "畏寒", "四肢不温", "喜暖", "面色苍白"],
        "min_match": 2,
    },
    "阴虚": {
        "keywords": ["手足心热", "口干", "潮热", "盗汗", "心烦", "颧红", "皮肤干燥"],
        "min_match": 2,
    },
    "气虚": {
        "keywords": ["乏力", "容易疲劳", "气短", "懒言", "自汗", "容易感冒", "没力气"],
        "min_match": 2,
    },
    "痰湿": {
        "keywords": ["身体沉重", "痰多", "胸闷", "肥胖", "口中黏腻", "舌苔厚"],
        "min_match": 2,
    },
    "湿热": {
        "keywords": ["面部油光", "口苦", "口臭", "大便黏滞", "小便黄", "容易长痘"],
        "min_match": 2,
    },
    "血瘀": {
        "keywords": ["肤色晦暗", "色素沉着", "容易出现瘀斑", "唇色暗紫", "痛经有血块"],
        "min_match": 2,
    },
    "气郁": {
        "keywords": ["叹气", "胸闷", "情绪低落", "容易焦虑", "胁肋胀痛", "多愁善感"],
        "min_match": 2,
    },
    "特禀": {
        "keywords": ["过敏", "打喷嚏", "皮肤敏感", "哮喘", "花粉"],
        "min_match": 2,
    },
    "平和": {
        "keywords": ["精力充沛", "睡眠好", "性格开朗", "很少生病"],
        "min_match": 3,
    },
}


async def _update_constitution_from_fragments(
    user_id: str, fragments: list
) -> None:
    """从记忆碎片中提取体质信息并更新用户画像"""
    from app.memory.store import memory as memory_store

    # 策略1：碎片中直接标注了constitution
    for frag in fragments:
        if (
            hasattr(frag, "constitution")
            and frag.constitution
            and frag.constitution not in ("无", None, "")
        ):
            constitution = frag.constitution
            profile = await memory_store.update_profile(
                user_id, constitution=constitution
            )
            logger.info(f"用户 {user_id}: 从碎片直接识别体质={constitution}")
            # 同步更新主要困扰
            if frag.category in ("health_fact", "emotion_pattern") and frag.tags:
                existing = set(profile.main_concerns or [])
                new_tags = set(frag.tags) - existing
                if new_tags:
                    updated = list(existing | new_tags)[:5]
                    await memory_store.update_profile(
                        user_id, main_concerns=updated
                    )
            return

    # 策略2：碎片内容中包含体质关键词，推断体质
    all_content = " ".join(f.content for f in fragments)
    best_match = None
    best_score = 0

    for constitution, pattern in CONSTITUTION_PATTERNS.items():
        matched = sum(1 for kw in pattern["keywords"] if kw in all_content)
        if matched >= pattern["min_match"] and matched > best_score:
            best_score = matched
            best_match = constitution

    if best_match:
        await memory_store.update_profile(user_id, constitution=best_match)
        logger.info(
            f"用户 {user_id}: 从症状推断体质={best_match} (匹配{best_score}个关键词)"
        )

    # 策略3：更新主要困扰（从碎片标签中提取）
    profile = await memory_store.get_profile(user_id)
    existing = set(profile.main_concerns or [])
    new_concerns = set()
    for frag in fragments:
        if frag.category in ("health_fact", "emotion_pattern", "relationship"):
            for tag in frag.tags:
                if tag not in existing and len(new_concerns) < 3:
                    new_concerns.add(tag)

    if new_concerns:
        updated = list(existing | new_concerns)[:5]
        await memory_store.update_profile(user_id, main_concerns=updated)


async def infer_constitution_from_dialogue(
    user_id: str, recent_messages: list[dict]
) -> str | None:
    """LLM驱动的体质推断（当关键词匹配不足时使用）

    Args:
        user_id: 用户ID
        recent_messages: 最近几轮对话 [{"role": "user/assistant", "content": "..."}]

    Returns:
        推断出的体质名称，或None
    """
    # 先检查是否已有体质
    from app.memory.store import memory as memory_store

    profile = await memory_store.get_profile(user_id)
    if profile.constitution and profile.constitution != "未测评":
        return profile.constitution  # 已有体质，不需要推断

    # 只用用户消息推断
    user_msgs = [
        m["content"] for m in recent_messages if m["role"] == "user"
    ]
    if len(user_msgs) < 3:
        return None  # 信息不足

    prompt = f"""根据以下用户对话内容，推断用户最可能的中医体质类型。

九种体质：阳虚、阴虚、气虚、痰湿、湿热、血瘀、气郁、特禀、平和

用户对话：
{chr(10).join(f'- {m}' for m in user_msgs[-8:])}

请只输出一个体质名称，不要解释。如果信息不足以判断，输出"无法判断"。"""

    try:
        settings = get_settings()
        extractor_model = settings.llm_extractor_model or None
        result = await chat(
            messages=[{"role": "user", "content": prompt}],
            model=extractor_model,
            max_tokens=20,
            temperature=0.1,
        )
        constitution = result.strip()
        if constitution in CONSTITUTION_PATTERNS:
            await memory_store.update_profile(
                user_id, constitution=constitution
            )
            logger.info(f"用户 {user_id}: LLM推断体质={constitution}")
            return constitution
    except Exception as e:
        logger.warning(f"体质推断失败: {e}")

    return None
