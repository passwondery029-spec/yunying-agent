"""Orchestrator — 意图路由 + 上下文组装 + 响应调度"""

from enum import Enum
from dataclasses import dataclass

from app.core.llm import chat_with_system, _has_api_key


class Intent(str, Enum):
    """用户意图分类"""
    HEALTH = "health"       # 健康咨询/数据解读
    HEALING = "healing"     # 情绪/心理/冥想
    PRODUCT = "product"     # 产品咨询/购买
    GENERAL = "general"     # 闲聊/其他


# 意图路由 Prompt
INTENT_ROUTER_PROMPT = """你是一个意图分类器。根据用户的消息，判断用户的主要意图。

分类规则：
- health：用户提到身体症状（头痛、胸闷、心慌、头晕等）、健康指标、心率、睡眠质量、体温、运动、养生、体质、中医身体概念（肝气、心火、阳虚等）
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


@dataclass
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
            memory_text=memory_text,
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
            memory_text=memory_text,
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
            memory_text=memory_text,
        )
        engine = "product"

    else:
        # 通用闲聊，走疗愈引擎的轻量模式
        # 不注入健康快照，不注入RAG，保持自然闲聊感
        # 但仍注入记忆碎片（让闲聊也能记住用户）
        from app.engines.healing.engine import healing_chat

        reply = await healing_chat(
            user_message=user_message,
            user_id=user_id,
            history=history,
            healing_snapshot=None,  # 闲聊不注入健康快照
            light_mode=True,  # 轻量模式：不注入知识库
            memory_text=memory_text,
        )
        engine = "healing(general)"

    # 3. 构建结果
    return OrchestratorResult(
        reply=reply,
        intent=intent,
        engine=engine,
        suggested_actions=[],
        product_recommendation=None,
    )
