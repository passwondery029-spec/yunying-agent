"""产品推荐引擎"""

from app.core.llm import chat_with_system
from app.core.rag import rag_service
from app.engines.product.prompts import PRODUCT_ENGINE_SYSTEM_PROMPT, PRODUCT_SNAPSHOT_TEMPLATE
from app.health.models import HealthMetrics, HealthEvent, UserHealthBaseline


def build_product_snapshot(
    metrics: HealthMetrics | None = None,
    baseline: UserHealthBaseline | None = None,
    events: list[HealthEvent] | None = None,
    constitution: str = "未测评",
    main_concern: str = "暂无",
    emotion_trend: str = "暂无数据",
    purchased_products: list[str] | None = None,
    already_recommended: bool = False,
) -> str:
    """构建产品推荐场景的用户状态快照"""

    if metrics is None:
        return "暂无用户数据"

    baseline = baseline or UserHealthBaseline(user_id="default")

    # 睡眠状况
    if metrics.sleep_duration_hours is not None:
        sleep = metrics.sleep_duration_hours
        if sleep < 5:
            sleep_status = "严重不足"
        elif sleep < 6:
            sleep_status = "不足"
        elif sleep < 7:
            sleep_status = "略少"
        else:
            sleep_status = "正常"
        sleep_hours = f"{sleep:.1f}"
    else:
        sleep_status = "未知"
        sleep_hours = "无数据"

    # 压力水平
    if metrics.hrv_sdnn is not None:
        hrv_ratio = metrics.hrv_sdnn / baseline.hrv_sdnn_baseline
        if hrv_ratio < 0.6:
            stress_level = "高"
        elif hrv_ratio < 0.8:
            stress_level = "中"
        else:
            stress_level = "低"
    else:
        stress_level = "未知"

    # 已购产品
    if purchased_products:
        products_str = "、".join(purchased_products)
    else:
        products_str = "无"

    return PRODUCT_SNAPSHOT_TEMPLATE.format(
        constitution=constitution,
        main_concern=main_concern,
        emotion_trend=emotion_trend,
        sleep_status=sleep_status,
        sleep_hours=sleep_hours,
        stress_level=stress_level,
        purchased_products=products_str,
        already_recommended="是" if already_recommended else "否",
    )


async def product_chat(
    user_message: str,
    user_id: str,
    history: list[dict] | None = None,
    product_snapshot: str | None = None,
    memory_text: str = "",
) -> str:
    """产品推荐引擎对话

    Args:
        user_message: 用户消息
        user_id: 用户 ID
        history: 对话历史
        product_snapshot: 产品推荐场景用户状态快照
        memory_text: 格式化后的记忆碎片文本

    Returns:
        引擎回复
    """
    system_prompt = PRODUCT_ENGINE_SYSTEM_PROMPT

    # 注入记忆碎片（在快照之前，让引擎先了解用户背景）
    if memory_text:
        system_prompt += f"\n\n{memory_text}"

    if product_snapshot and product_snapshot != "暂无用户数据":
        system_prompt += f"\n\n{product_snapshot}"

    # 注入中医心理学知识库（产品推荐重点关注药物疗法、体质调养）
    context_messages = history or []
    context_messages = context_messages + [{"role": "user", "content": user_message}]
    knowledge_section = rag_service.retrieve(
        messages=context_messages,
        engine_type="product",
        max_chars=2000,
    )
    if knowledge_section:
        system_prompt += f"\n\n## 中医心理学参考知识（用于专业推荐支撑）\n{knowledge_section}"

    return await chat_with_system(
        system_prompt=system_prompt,
        user_message=user_message,
        history=history,
        temperature=0.65,  # 推荐场景需要更稳定
        max_tokens=400,    # 推荐回复不需要太长
    )
