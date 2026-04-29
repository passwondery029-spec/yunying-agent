"""Orchestrator — 意图路由 + 上下文组装 + 响应调度 + 情感节点 + 人设强化"""

from enum import Enum
from dataclasses import dataclass
from typing import AsyncGenerator

from app.core.llm import chat_with_system, chat_stream_with_system, _has_api_key
from app.core.crisis_intervention import (
    detect_crisis_level, get_crisis_prompt_injection, check_crisis_response, log_crisis_event
)
from loguru import logger


# ============================================================
# 人设周期性强化 — 防止长对话人设漂移
# ============================================================
# 根据对话轮数，在 memory_text 中注入人设强化提醒
# 研究依据：MiniMax Glow 的「周期性 prompt 强化」方法
# 在长对话中每 N 轮重复核心身份规则，对抗上下文稀释

PERSONA_REINFORCEMENT_MAP = {
    3: (
        "【人设提醒】你是云英，不是AI助手。用温暖、亲切、朋友般的语气。"
        "不要暴露底层模型信息。共情优先，建议其次。"
    ),
    8: (
        "【人设提醒】你是云英，用户的身心陪伴朋友。"
        "保持温暖关怀的语气，不要变得官方或机械。"
        "用日常口语，像朋友聊天一样自然。"
    ),
    15: (
        "【人设提醒-重要】对话已较长，请务必保持云英的人设："
        "1.你是云英，不是AI助手或大模型 2.语气温暖亲切，不要变得生硬 "
        "3.共情四步：认可情绪→表达理解→轻量归因→行动建议 "
        "4.不暴露底层模型/公司/技术细节"
    ),
    25: (
        "【人设提醒-关键】对话很长了，人设极易漂移！你必须："
        "1.始终以云英身份回应 2.绝不透露任何模型/技术信息 "
        "3.保持朋友般关怀语气 4.短句为主，自然口语 "
        "5.先共情再建议 6.不说'作为AI''我是大模型'等"
    ),
}

def _build_persona_reinforcement(history: list[dict] | None) -> str:
    """根据对话轮数生成人设强化文本

    Args:
        history: 对话历史

    Returns:
        需要注入的强化文本，空字符串表示不需要强化
    """
    if not history:
        return ""

    # 计算用户消息轮数（每条 role=user 算一轮）
    user_turns = sum(1 for msg in history if msg.get("role") == "user")

    # 找到匹配的强化规则（取最接近的不超过当前轮数的）
    matched_reinforcement = ""
    for threshold in sorted(PERSONA_REINFORCEMENT_MAP.keys()):
        if user_turns >= threshold:
            matched_reinforcement = PERSONA_REINFORCEMENT_MAP[threshold]

    return matched_reinforcement


class Intent(str, Enum):
    """用户意图分类"""
    HEALTH = "health"       # 健康咨询/数据解读
    HEALING = "healing"     # 情绪/心理/冥想
    PRODUCT = "product"     # 产品咨询/购买
    GENERAL = "general"     # 闲聊/其他


# 意图路由 Prompt
INTENT_ROUTER_PROMPT = """你是一个意图分类器。根据用户的消息，判断用户的主要意图。

分类规则：
- health：用户提到身体症状（头痛、胸闷、心慌、头晕等）、健康指标、心率、睡眠质量、体温、运动、养生、体质、中医身体概念（肝气、心火、阳虚等）、询问自己最近状况（"我最近怎么样""我最近身体怎么样""看看我的数据""我的健康情况"）
- healing：用户提到情绪困扰（焦虑、压力、低落、悲伤、愤怒、恐惧等）、想倾诉、冥想、放松、心情不好、孤独、想哭、失眠伴随情绪问题、更年期情绪波动
- product：用户问到产品、购买、推荐商品、价格、下单、合香、手串、香、订、买、多少钱、沉香、檀香
- general：日常问候、闲聊、天气、其他无关话题

优先级规则：
- 如果用户同时提到健康和情绪，优先归类为 healing（情绪通常是更深层的需求）
- 如果用户描述身体症状但明显由情绪引发（如"生气头疼""焦虑心慌"），归类为 healing
- 如果用户提到失眠但伴随情绪描述（如"烦得睡不着""想太多睡不着"），归类为 healing
- 如果用户提到"安神助眠"且在问产品，归类为 product
- 如果用户只是在闲聊中随口提到健康词汇（如"我今天挺好的"），归类为 general

只返回一个分类词，不要解释。

返回格式：只返回 health / healing / product / general 中的一个"""


# 情绪提取关键词
_EMOTION_KEYWORDS = {
    "焦虑": "焦虑", "压力": "压力", "烦": "烦躁", "累": "疲惫",
    "难过": "难过", "伤心": "伤心", "抑郁": "低落", "害怕": "恐惧",
    "孤独": "孤独", "不开心": "不开心", "心烦": "心烦", "委屈": "委屈",
    "想哭": "想哭", "哭": "悲伤", "失眠": "失眠困扰", "紧张": "紧张",
    "害怕": "恐惧", "暴躁": "暴躁", "低落": "低落", "郁闷": "郁闷",
    "喘不过气": "压力过大", "睡不着": "失眠困扰", "半夜醒": "失眠困扰",
}


def _extract_emotional_node(user_message: str) -> tuple[str, str] | None:
    """从用户消息中提取关键情绪事件

    Returns:
        (情绪标签, 事件描述) 或 None
    """
    msg = user_message.lower()

    for keyword, emotion in _EMOTION_KEYWORDS.items():
        if keyword in msg:
            # 提取简短描述（最多40字）
            description = user_message[:40] if len(user_message) <= 40 else user_message[:37] + "..."
            return (emotion, description)

    return None



class OrchestratorResult:
    """调度结果"""
    reply: str
    intent: Intent
    engine: str          # 实际处理引擎名
    suggested_actions: list[dict] = None
    product_recommendation: dict | None = None


async def classify_intent(user_message: str, health_events: list | None = None) -> Intent:
    """分类用户意图

    Args:
        user_message: 用户消息
        health_events: 当前活跃的健康事件（影响路由决策）

    Returns:
        意图分类
    """
    # 如果有高严重度的健康事件，优先走健康引擎
    if health_events:
        high_severity = [e for e in health_events if e.severity.value == "high"]
        if high_severity:
            return Intent.HEALTH

    # 无 API Key 时用关键词匹配降级
    if not _has_api_key:
        return _keyword_classify(user_message)

    # LLM 意图分类
    result = await chat_with_system(
        system_prompt=INTENT_ROUTER_PROMPT,
        user_message=user_message,
        temperature=0.1,  # 低温度，更确定性的分类
        max_tokens=16,
    )

    result = result.strip().lower()

    # 映射到意图
    intent_map = {
        "health": Intent.HEALTH,
        "healing": Intent.HEALING,
        "product": Intent.PRODUCT,
        "general": Intent.GENERAL,
    }

    return intent_map.get(result, Intent.GENERAL)


def _keyword_classify(message: str) -> Intent:
    """关键词匹配意图分类（无 LLM 时的降级方案）"""
    msg = message.lower()

    health_keywords = [
        "心率", "血压", "睡眠", "体温", "步数", "运动", "养生",
        "体检", "指标", "数据", "心跳", "身体", "不舒服", "头晕",
        "头痛", "胃", "腰", "颈椎", "关节", "感冒", "咳嗽",
        "体质", "阳虚", "阴虚", "气虚", "痰湿", "气郁",
        "肝气", "心火", "脾虚", "肾虚", "肺气",
        "穴位", "按摩", "针灸", "方剂", "中药",
        "我最近怎么样", "我最近身体", "看看我的数据", "我的健康",
        "我的指标", "我的睡眠", "我的心率", "最近状况",
    ]
    healing_keywords = [
        "焦虑", "压力", "烦", "累", "心情", "情绪", "冥想", "放松",
        "失眠", "紧张", "难过", "伤心", "抑郁", "害怕", "孤独",
        "不开心", "心烦", "暴躁", "倾诉", "想聊天", "陪我说",
        "疗愈", "治愈", "呼吸", "静心", "正念",
        "叹气", "胸闷", "堵", "委屈", "想哭", "哭",
        "更年期", "胆怯", "自卑", "恍惚", "梅核",
        "什么都不想", "纠结", "思虑", "反复",
        "生气", "发火", "愤怒", "怒", "委屈",
        "嗓子堵", "咽不下", "烦躁", "头疼",
    ]
    product_keywords = [
        "产品", "购买", "推荐", "价格", "下单", "商品", "香", "合香",
        "手串", "订", "买", "多少钱", "沉香", "檀香", "安神",
        "助眠香", "云眠", "静夜", "云舒", "自在", "晨曦", "清心",
    ]

    # 情绪-身体混合信号的强规则：如果同时出现情绪词和身体词，优先 healing
    emotion_signal_words = [
        "生气", "发火", "愤怒", "烦", "压力", "焦虑", "郁闷",
        "更年期", "委屈", "心烦", "暴躁", "想哭", "难过",
    ]
    has_emotion_signal = any(kw in msg for kw in emotion_signal_words)

    # 计算各分类匹配数
    scores = {
        Intent.HEALTH: sum(1 for k in health_keywords if k in msg),
        Intent.HEALING: sum(1 for k in healing_keywords if k in msg),
        Intent.PRODUCT: sum(1 for k in product_keywords if k in msg),
    }

    max_score = max(scores.values())
    if max_score == 0:
        return Intent.GENERAL

    # healing 优先级高于 health（情绪是更深层需求）
    if scores[Intent.HEALING] > 0 and scores[Intent.HEALING] >= scores[Intent.HEALTH]:
        return Intent.HEALING

    # 情绪-身体混合信号：有情绪信号时，即使 health 分数更高也走 healing
    if has_emotion_signal and scores[Intent.HEALTH] > 0:
        return Intent.HEALING

    return max(scores, key=scores.get)


def _build_health_snapshot_for_engine(
    profile=None,
    health_events=None,
) -> str:
    """构建健康快照供 Health Engine 使用"""
    from app.engines.health.engine import build_health_snapshot
    from app.memory.store import memory

    if profile is None:
        return "暂无用户健康数据"

    # 从记忆系统获取最新健康指标
    metrics = memory.get_metrics(profile.user_id)

    return build_health_snapshot(
        metrics=metrics,
        baseline=profile.baseline,
        events=health_events,
        emotion_trend=profile.emotion_trend,
        last_meditation=profile.last_meditation,
    )


def _build_healing_snapshot_for_engine(
    profile=None,
    health_events=None,
) -> str:
    """构建疗愈快照供 Healing Engine 使用"""
    from app.engines.healing.engine import build_healing_snapshot
    from app.memory.store import memory

    if profile is None:
        return "暂无用户身心数据"

    metrics = memory.get_metrics(profile.user_id)

    return build_healing_snapshot(
        metrics=metrics,
        baseline=profile.baseline,
        events=health_events,
        emotion_trend=profile.emotion_trend,
        last_meditation=profile.last_meditation,
    )


def _build_product_snapshot_for_engine(
    profile=None,
    health_events=None,
) -> str:
    """构建产品快照供 Product Engine 使用"""
    from app.engines.product.engine import build_product_snapshot
    from app.memory.store import memory

    if profile is None:
        return "暂无用户数据"

    metrics = memory.get_metrics(profile.user_id)
    main_concern = "、".join(profile.main_concerns) if profile.main_concerns else "暂无"

    return build_product_snapshot(
        metrics=metrics,
        baseline=profile.baseline,
        events=health_events,
        constitution=profile.constitution,
        main_concern=main_concern,
        emotion_trend=profile.emotion_trend,
        purchased_products=profile.purchased_products,
        already_recommended=profile.already_recommended,
    )


async def orchestrate(
    user_message: str,
    user_id: str,
    history: list[dict] | None = None,
    health_snapshot: str | None = None,
    health_events: list | None = None,
    # 用户画像（用于构建快照）
    profile=None,
    # 记忆碎片文本（已格式化）
    memory_text: str = "",
) -> OrchestratorResult:
    """调度用户消息到合适的引擎

    Args:
        user_message: 用户消息
        user_id: 用户 ID
        history: 对话历史
        health_snapshot: 健康快照（已有则直接用，否则按需构建）
        health_events: 活跃健康事件
        profile: 用户画像（UserProfile），用于构建各引擎快照
        memory_text: 格式化后的记忆碎片文本，注入到引擎 system_prompt

    Returns:
        调度结果
    """
    # 1. 意图分类
    intent = await classify_intent(user_message, health_events)

    # 1.1 危机干预检测 — 法规第十三条合规
    crisis_level, crisis_score = detect_crisis_level(user_message)
    crisis_injection = get_crisis_prompt_injection(crisis_level)
    if crisis_score > 0:
        logger.warning("检测到用户{}危机等级: {} 消息: {}", user_id, crisis_level, user_message[:50])

    # 1.5 情感节点：提取并注入
    from app.memory.store import memory as mem_store
    emotional_context = ""
    if profile and hasattr(profile, 'user_id'):
        # 提取情绪事件
        extracted = _extract_emotional_node(user_message)
        if extracted:
            emotion, description = extracted
            mem_store.add_emotional_node(profile.user_id, description, emotion)

        # 获取情感上下文
        emotional_context = mem_store.build_emotional_context(profile.user_id)

    # 1.6 关系层级：更新积分 + 获取 prompt 注入文本
    relationship_context = ""
    if profile and hasattr(profile, 'user_id'):
        rel, just_leveled = mem_store.update_relationship_score(profile.user_id, user_message)
        relationship_context = mem_store.get_relationship_prompt(profile.user_id)
        if just_leveled:
            logger.info("用户 {} 关系升级到: {}", profile.user_id, rel.level.value)

    # 1.7 健康趋势分析
    trend_context = ""
    if profile and hasattr(profile, 'user_id'):
        from app.core.health_trend import analyze_trend, build_trend_prompt
        from app.memory.store import memory as mem_store
        # 获取最近两次健康数据
        recent_metrics = mem_store.get_recent_metrics(profile.user_id, limit=2)
        if len(recent_metrics) >= 1:
            current_data = recent_metrics[0] if recent_metrics else None
            previous_data = recent_metrics[1] if len(recent_metrics) >= 2 else None
            trends = analyze_trend(current_data, previous_data)
            trend_context = build_trend_prompt(trends)
            # 异常指标触发情感节点
            for t in trends:
                if t.alert:
                    mem_store.add_emotional_node(
                        profile.user_id,
                        f"健康指标异常: {t.detail}",
                        "health_alert"
                    )

    # 合并 memory_text + emotional_context + relationship_context + trend_context + persona_reinforcement
    full_memory = ""
    if memory_text:
        full_memory = memory_text
    if emotional_context:
        full_memory = f"{emotional_context}\n\n{full_memory}" if full_memory else emotional_context
    if relationship_context:
        full_memory = f"{relationship_context}\n\n{full_memory}" if full_memory else relationship_context
    if trend_context:
        full_memory = f"{trend_context}\n\n{full_memory}" if full_memory else trend_context
    # 周期性人设强化
    persona_reinforcement = _build_persona_reinforcement(history)
    if persona_reinforcement:
        full_memory = f"{full_memory}\n\n{persona_reinforcement}" if full_memory else persona_reinforcement
    # 用户画像注入
    if profile:
        from app.core.user_profile import profile_to_dict, profile_to_prompt_text
        profile_text = profile_to_prompt_text(profile_to_dict(profile))
        if profile_text:
            full_memory = f"{profile_text}\n\n{full_memory}" if full_memory else profile_text
    # 危机提示注入
    if crisis_injection:
        full_memory = f"{crisis_injection}\n\n{full_memory}" if full_memory else crisis_injection

    # 2. 路由到对应引擎
    if intent == Intent.HEALTH:
        from app.engines.health.engine import health_chat

        # Health 引擎：需要健康快照
        if not health_snapshot:
            health_snapshot = _build_health_snapshot_for_engine(
                profile=profile,
                health_events=health_events,
            )

        reply = await health_chat(
            user_message=user_message,
            user_id=user_id,
            history=history,
            health_snapshot=health_snapshot,
            memory_text=full_memory,
        )
        engine = "health"

    elif intent == Intent.HEALING:
        from app.engines.healing.engine import healing_chat

        # Healing 引擎：需要疗愈快照
        healing_snapshot = _build_healing_snapshot_for_engine(
            profile=profile,
            health_events=health_events,
        )

        reply = await healing_chat(
            user_message=user_message,
            user_id=user_id,
            history=history,
            healing_snapshot=healing_snapshot,
            memory_text=full_memory,
        )
        engine = "healing"

    elif intent == Intent.PRODUCT:
        from app.engines.product.engine import product_chat

        # Product 引擎：需要产品快照（含体质、已购产品等）
        product_snapshot = _build_product_snapshot_for_engine(
            profile=profile,
            health_events=health_events,
        )

        reply = await product_chat(
            user_message=user_message,
            user_id=user_id,
            history=history,
            product_snapshot=product_snapshot,
            memory_text=full_memory,
        )
        engine = "product"

    else:
        # 通用闲聊，走疗愈引擎的轻量模式
        from app.engines.healing.engine import healing_chat

        light_snapshot = _build_healing_snapshot_for_engine(
            profile=profile,
            health_events=health_events,
        ) if profile else None

        reply = await healing_chat(
            user_message=user_message,
            user_id=user_id,
            history=history,
            healing_snapshot=light_snapshot,
            light_mode=True,
            memory_text=full_memory,
        )
        engine = "healing(general)"

    # 3. Persona Guard — 人设输出校验
    from app.core.persona_guard import check_persona
    passed, reply = check_persona(reply, user_message=user_message)
    if not passed:
        logger.warning("Persona guard corrected reply for user {}", user_id)

    # 4. 用户画像自动提取（后台异步，不阻塞回复）
    try:
        from app.core.user_profile import extract_profile_updates, apply_updates_to_profile
        profile_dict = {}
        if profile:
            from app.core.user_profile import profile_to_dict
            profile_dict = profile_to_dict(profile)
        # 提取更新（异步，不阻塞）
        import asyncio
        asyncio.create_task(_update_user_profile_background(
            user_id=user_id,
            profile=profile,
            profile_dict=profile_dict,
            history=history,
        ))
    except Exception as e:
        logger.debug(f"画像提取调度失败（不影响回复）: {e}")

    # 5. 构建结果
    return OrchestratorResult(
        reply=reply,
        intent=intent,
        engine=engine,
        suggested_actions=[],
        product_recommendation=None,
    )


async def orchestrate_stream(
    user_message: str,
    user_id: str,
    history: list[dict] | None = None,
    health_snapshot: str | None = None,
    health_events: list | None = None,
    profile=None,
    memory_text: str = "",
) -> AsyncGenerator[str, None]:
    """流式调度：先意图分类（非流式），再流式生成回复

    Yields:
        文本片段
    """
    # 1. 意图分类（非流式，因为只需要一个词）
    intent = await classify_intent(user_message, health_events)

    # 1.1 危机干预检测
    crisis_level, crisis_score = detect_crisis_level(user_message)
    crisis_injection = get_crisis_prompt_injection(crisis_level)
    if crisis_score > 0:
        logger.warning("流式-检测到用户{}危机等级: {} 消息: {}", user_id, crisis_level, user_message[:50])

    # 1.5 情感节点：提取并注入
    from app.memory.store import memory as mem_store
    emotional_context = ""
    if profile and hasattr(profile, 'user_id'):
        extracted = _extract_emotional_node(user_message)
        if extracted:
            emotion, description = extracted
            mem_store.add_emotional_node(profile.user_id, description, emotion)
        emotional_context = mem_store.build_emotional_context(profile.user_id)

    # 1.6 关系层级：更新积分 + 获取 prompt 注入文本
    relationship_context = ""
    if profile and hasattr(profile, 'user_id'):
        rel, just_leveled = mem_store.update_relationship_score(profile.user_id, user_message)
        relationship_context = mem_store.get_relationship_prompt(profile.user_id)
        if just_leveled:
            logger.info("用户 {} 关系升级到: {}", profile.user_id, rel.level.value)

    # 1.7 健康趋势分析（流式）
    trend_context = ""
    if profile and hasattr(profile, 'user_id'):
        from app.core.health_trend import analyze_trend, build_trend_prompt
        recent_metrics = mem_store.get_recent_metrics(profile.user_id, limit=2)
        if len(recent_metrics) >= 1:
            current_data = recent_metrics[0] if recent_metrics else None
            previous_data = recent_metrics[1] if len(recent_metrics) >= 2 else None
            trends = analyze_trend(current_data, previous_data)
            trend_context = build_trend_prompt(trends)
            for t in trends:
                if t.alert:
                    mem_store.add_emotional_node(
                        profile.user_id,
                        f"健康指标异常: {t.detail}",
                        "health_alert"
                    )

    # 合并 memory_text + emotional_context + relationship_context + trend_context + persona_reinforcement
    full_memory = ""
    if memory_text:
        full_memory = memory_text
    if emotional_context:
        full_memory = f"{emotional_context}\n\n{full_memory}" if full_memory else emotional_context
    if relationship_context:
        full_memory = f"{relationship_context}\n\n{full_memory}" if full_memory else relationship_context
    if trend_context:
        full_memory = f"{trend_context}\n\n{full_memory}" if full_memory else trend_context
    # 周期性人设强化
    persona_reinforcement = _build_persona_reinforcement(history)
    if persona_reinforcement:
        full_memory = f"{full_memory}\n\n{persona_reinforcement}" if full_memory else persona_reinforcement
    # 用户画像注入
    if profile:
        from app.core.user_profile import profile_to_dict, profile_to_prompt_text
        profile_text = profile_to_prompt_text(profile_to_dict(profile))
        if profile_text:
            full_memory = f"{profile_text}\n\n{full_memory}" if full_memory else profile_text
    # 危机提示注入
    if crisis_injection:
        full_memory = f"{crisis_injection}\n\n{full_memory}" if full_memory else crisis_injection

    # 2. 流式路由到对应引擎
    if intent == Intent.HEALTH:
        from app.engines.health.engine import health_chat_stream

        if not health_snapshot:
            health_snapshot = _build_health_snapshot_for_engine(
                profile=profile, health_events=health_events,
            )

        async for chunk in health_chat_stream(
            user_message=user_message,
            user_id=user_id,
            history=history,
            health_snapshot=health_snapshot,
            memory_text=full_memory,
        ):
            yield chunk

    elif intent == Intent.HEALING:
        from app.engines.healing.engine import healing_chat_stream

        healing_snapshot = _build_healing_snapshot_for_engine(
            profile=profile, health_events=health_events,
        )

        async for chunk in healing_chat_stream(
            user_message=user_message,
            user_id=user_id,
            history=history,
            healing_snapshot=healing_snapshot,
            memory_text=full_memory,
        ):
            yield chunk

    elif intent == Intent.PRODUCT:
        from app.engines.product.engine import product_chat_stream

        product_snapshot = _build_product_snapshot_for_engine(
            profile=profile, health_events=health_events,
        )

        async for chunk in product_chat_stream(
            user_message=user_message,
            user_id=user_id,
            history=history,
            product_snapshot=product_snapshot,
            memory_text=full_memory,
        ):
            yield chunk

    else:
        # 通用闲聊：仍注入基础健康快照
        from app.engines.healing.engine import healing_chat_stream

        light_snapshot = _build_healing_snapshot_for_engine(
            profile=profile, health_events=health_events,
        ) if profile else None

        async for chunk in healing_chat_stream(
            user_message=user_message,
            user_id=user_id,
            history=history,
            healing_snapshot=light_snapshot,
            light_mode=True,
            memory_text=full_memory,
        ):
            yield chunk

    # 流式完成后也做画像提取（后台异步）
    try:
        from app.core.user_profile import extract_profile_updates, apply_updates_to_profile, profile_to_dict
        import asyncio
        if profile:
            asyncio.create_task(_update_user_profile_background(
                user_id=user_id,
                profile=profile,
                profile_dict=profile_to_dict(profile),
                history=history,
            ))
    except Exception:
        pass


async def _update_user_profile_background(
    user_id: str,
    profile,
    profile_dict: dict,
    history: list[dict],
):
    """后台异步更新用户画像，不阻塞回复"""
    try:
        from app.core.llm import _get_client
        client = _get_client()
        if not client:
            return

        updates = await extract_profile_updates(
            llm_client=client,
            current_profile=profile_dict,
            recent_messages=history[-10:],
        )

        if updates and profile:
            apply_updates_to_profile(profile, updates)
            # 持久化
            from app.memory.store import MemoryStore
            mem_store = MemoryStore()
            await mem_store.update_profile(user_id, **updates)
            logger.info(f"📊 用户画像已更新: {updates}")

    except Exception as e:
        logger.debug(f"画像后台更新失败（不影响使用）: {e}")
