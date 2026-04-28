"""LLM 统一调用封装

支持两种模式：
- 有 API Key：真实调用 LLM（含重试、降级、超时）
- 无 API Key：降级为模拟回复（用于开发和测试）

支持流式输出：
- chat_stream(): 异步生成器，逐 token 返回

安全措施：
- API Key 不出现在日志中
- 调用失败自动重试（指数退避）
- 重试耗尽后降级回复
"""

import asyncio
import time
from typing import AsyncGenerator
from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError
from loguru import logger
from app.config import get_settings

_settings = get_settings()

# 判断是否有可用的 API Key
_has_api_key = bool(_settings.llm_api_key and _settings.llm_api_key not in ("", "your-api-key-here"))

_client = AsyncOpenAI(
    api_key=_settings.llm_api_key or "placeholder",
    base_url=_settings.llm_base_url,
    timeout=30.0,  # 单次请求超时30秒
    max_retries=0,  # 我们自己控制重试逻辑
) if _has_api_key else None

# 模型别名映射
MODEL_ALIASES = {
    "doubao": "doubao-seed-1-6-flash-250828",
    "doubao-pro": "doubao-seed-2-0-pro-260215",
    "doubao-lite": "doubao-seed-2-0-lite-260215",
    "deepseek": "deepseek-v3-1-250821",
}

# 模型降级链：主模型→备用模型→轻量模型
# 格式：[(model_name, max_retries, timeout), ...]
FALLBACK_CHAIN = [
    (_settings.llm_model, 2, 30.0),                          # 主模型：接入点ID
    ("ep-m-20260305204118-rh2xg", 1, 20.0),                  # 同接入点降级重试
]

# 重试配置
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # 首次重试等待1秒
RETRY_MAX_DELAY = 10.0  # 最大等待10秒

# 降级回复模板
FALLBACK_REPLIES = {
    "error": "抱歉，我现在有点忙不过来，请稍等再试一次。",
    "timeout": "我思考了太久了，请再说一次好吗？",
    "connection": "网络好像有点问题，请稍后再试。",
    "rate_limit": "我现在响应的人有点多，请稍等一下再试。",
}


def _mock_reply(user_message: str) -> str:
    """无 API Key 时的模拟回复，用于开发测试"""
    msg = user_message.lower()
    if any(w in msg for w in ["睡眠", "睡不好", "失眠", "熬夜"]):
        return (
            "我理解睡眠不好确实很让人烦恼。"
            "根据中医理论，睡眠问题通常与心神不宁、肝火上扰有关。\n\n"
            "建议您可以试试：\n"
            "1. 睡前用温水泡脚 15-20 分钟\n"
            "2. 练习 5 分钟腹式呼吸冥想\n"
            "3. 避免睡前 1 小时使用手机\n\n"
            "您愿意试试今晚做个简单的睡前冥想吗？我可以引导您。"
        )
    if any(w in msg for w in ["心", "焦虑", "压力", "烦", "累", "不舒服"]):
        return (
            "听起来您最近承受了不少压力，辛苦了。\n\n"
            "我注意到您的身体数据也有一些变化，让我们一起关注一下。"
            "深呼吸，先让自己放松下来。\n\n"
            "现在，可以试着闭上眼睛，做 3 次深呼吸……"
        )
    if any(w in msg for w in ["你好", "嗨", "早上好", "晚上好", "hi", "hello"]):
        return (
            "您好！我是云英，您的身心健康伴侣。"
            "今天感觉怎么样？有什么想聊的吗？"
        )
    return (
        "谢谢您的分享。作为您的健康伴侣，我会一直在这里陪伴您。"
        "您有什么想聊的，或者身体上有什么不舒服，都可以跟我说。"
    )


def _get_fallback_reply(error_type: str = "error") -> str:
    """根据错误类型返回降级回复"""
    return FALLBACK_REPLIES.get(error_type, FALLBACK_REPLIES["error"])


async def _try_single_model(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    max_retries: int,
    timeout: float,
) -> tuple[str | None, str | None]:
    """尝试用单个模型调用，返回 (reply, error_type)"""
    for attempt in range(max_retries):
        try:
            # 临时修改超时
            old_timeout = client.timeout
            client.timeout = timeout
            start_time = time.time()
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            client.timeout = old_timeout
            elapsed = time.time() - start_time
            logger.debug(
                "LLM调用成功",
                extra={
                    "model": model,
                    "attempt": attempt + 1,
                    "elapsed_ms": int(elapsed * 1000),
                    "tokens_used": getattr(response.usage, "total_tokens", None),
                },
            )
            return response.choices[0].message.content or "", None

        except APITimeoutError:
            logger.warning(f"LLM超时(第{attempt+1}/{max_retries}次): model={model}")
            if attempt < max_retries - 1:
                delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                await asyncio.sleep(delay)
            else:
                return None, "timeout"

        except APIConnectionError:
            logger.warning(f"LLM连接失败(第{attempt+1}/{max_retries}次): model={model}")
            if attempt < max_retries - 1:
                delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                await asyncio.sleep(delay)
            else:
                return None, "connection"

        except APIStatusError as e:
            if e.status_code == 429:
                logger.warning(f"LLM限流(第{attempt+1}/{max_retries}次): model={model}")
                if attempt < max_retries - 1:
                    delay = min(RETRY_BASE_DELAY * (2 ** (attempt + 1)), RETRY_MAX_DELAY)
                    await asyncio.sleep(delay)
                else:
                    return None, "rate_limit"
            elif 400 <= e.status_code < 500:
                logger.error(f"LLM客户端错误(不重试): status={e.status_code} model={model}")
                return None, "client_error"
            else:
                logger.warning(f"LLM服务端错误(第{attempt+1}次): status={e.status_code} model={model}")
                if attempt < max_retries - 1:
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                    await asyncio.sleep(delay)
                else:
                    return None, "server_error"

        except Exception as e:
            logger.error(f"LLM未知异常: {type(e).__name__}: {e}")
            if attempt < max_retries - 1:
                delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                await asyncio.sleep(delay)
            else:
                return None, "error"

    return None, "error"


async def chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    **kwargs,
) -> str:
    """调用 LLM 生成回复（含降级链：主模型→备用模型→轻量模型→降级回复）

    降级策略：
    1. 主模型（配置的默认模型）→ 重试2次
    2. 备用模型（doubao-flash）→ 重试1次
    3. 轻量模型（doubao-lite）→ 重试1次
    4. 全部失败 → 降级回复模板

    Args:
        messages: OpenAI 格式的消息列表
        model: 模型名称（指定后只使用该模型，不走降级链）
        temperature: 温度参数
        max_tokens: 最大生成 token 数

    Returns:
        LLM 生成的文本
    """
    # 无 API Key 时降级为模拟回复
    if not _has_api_key or _client is None:
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        return _mock_reply(user_msg)

    # 指定了具体模型：只使用该模型（兼容旧行为）
    if model:
        model = MODEL_ALIASES.get(model, model)
        reply, error_type = await _try_single_model(
            _client, model, messages, temperature, max_tokens, MAX_RETRIES, 30.0
        )
        if reply is not None:
            return reply
        return _get_fallback_reply(error_type or "error")

    # 走降级链
    for chain_model, chain_retries, chain_timeout in FALLBACK_CHAIN:
        resolved_model = MODEL_ALIASES.get(chain_model, chain_model)
        logger.info(f"尝试模型: {resolved_model} (重试{chain_retries}次, 超时{chain_timeout}s)")
        reply, error_type = await _try_single_model(
            _client, resolved_model, messages, temperature, max_tokens, chain_retries, chain_timeout
        )
        if reply is not None:
            return reply
        logger.warning(f"模型 {resolved_model} 失败({error_type})，尝试下一个")

    # 全部模型都失败，返回降级回复
    logger.error("所有模型均失败，返回降级回复")
    return _get_fallback_reply(error_type or "error")


async def chat_stream(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    **kwargs,
) -> AsyncGenerator[str, None]:
    """流式调用 LLM，逐 token 返回文本片段

    Args:
        messages: OpenAI 格式的消息列表
        model: 模型名称
        temperature: 温度参数
        max_tokens: 最大生成 token 数

    Yields:
        文本片段（通常是一个 token 对应的几个字）
    """
    # 无 API Key 时降级为模拟回复（一次性 yield）
    if not _has_api_key or _client is None:
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        yield _mock_reply(user_msg)
        return

    resolved_model = model or _settings.llm_model
    resolved_model = MODEL_ALIASES.get(resolved_model, resolved_model)

    try:
        start_time = time.time()
        stream = await _client.chat.completions.create(
            model=resolved_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
        elapsed = time.time() - start_time
        logger.debug(f"LLM流式调用完成: model={resolved_model}, elapsed={elapsed:.1f}s")
    except Exception as e:
        logger.error(f"LLM流式调用失败: {type(e).__name__}: {e}")
        yield _get_fallback_reply("error")


async def chat_stream_with_system(
    system_prompt: str,
    user_message: str,
    history: list[dict] | None = None,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> AsyncGenerator[str, None]:
    """带系统提示的流式对话

    Args:
        system_prompt: 系统提示
        user_message: 用户消息
        history: 对话历史
        model: 模型名称
        temperature: 温度参数
        max_tokens: 最大 token 数

    Yields:
        文本片段
    """
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    async for chunk in chat_stream(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    ):
        yield chunk


async def chat_with_system(
    system_prompt: str,
    user_message: str,
    history: list[dict] | None = None,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> str:
    """带系统提示的快捷对话

    Args:
        system_prompt: 系统提示
        user_message: 用户消息
        history: 对话历史
        model: 模型名称
        temperature: 温度参数
        max_tokens: 最大 token 数

    Returns:
        LLM 生成的文本
    """
    messages = [{"role": "system", "content": system_prompt}]

    if history:
        messages.extend(history)

    messages.append({"role": "user", "content": user_message})

    return await chat(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )
