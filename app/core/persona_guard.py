"""Persona Guard — 人设输出校验层

对 LLM 已生成的回复做轻量校验，防止人设崩塌。
不做重生成，只做修补（删除/替换违规内容）。
"""

import re

# === 身份泄露检测规则 ===

# 底层模型名/公司名
_MODEL_LEAK_PATTERNS = [
    r"(?:我是|我是由|由|基于|powered by|built with|based on)\s*(?:豆包|doubao|GPT|ChatGPT|OpenAI|字节|ByteDance|智谱|GLM|Zhipu|通义|Qwen|千问|文心|Ernie|百度|Baidu|Kimi|Moonshot|DeepSeek|Claude|Anthropic|Gemini|Google|Llama|Meta\s*AI)",
    r"(?:doubao|gpt-4|gpt-3\.5|claude-3|gemini-pro|llama-3)\b",
    r"(?:大语言模型|大型语言模型|LLM|large language model)",
    r"(?:AI模型|人工智能模型|语言模型)\s*(?:叫|名为|是)",
]

# 系统提示词泄露
_SYSTEM_LEAK_PATTERNS = [
    r"(?:system\s*prompt|系统提示|system message|指令|instruction)",
    r"(?:作为AI|作为一个AI|我是AI助手|我是人工智能)",
    r"(?:我的训练数据|我的知识截止|我的数据)",
]

# 不符合人设的语气
_TONE_VIOLATION_PATTERNS = [
    r"(?:你应该|你必须|你需要|你一定要)",  # 不应该对用户用命令语气
    r"(?:别想太多|看开点|想开点|别矫情|矫情)",  # 否定感受
]


def check_persona(reply: str) -> tuple[bool, str]:
    """校验回复是否符合人设

    Args:
        reply: LLM 生成的回复

    Returns:
        (是否通过, 修正后的回复)
    """
    modified = reply
    has_fix = False

    # 1. 身份泄露检测
    for pattern in _MODEL_LEAK_PATTERNS + _SYSTEM_LEAK_PATTERNS:
        match = re.search(pattern, modified, re.IGNORECASE)
        if match:
            # 删除泄露句子（包含泄露词的整个句子）
            # 找到包含匹配的句子
            sentences = re.split(r'([。！？\n])', modified)
            new_sentences = []
            i = 0
            skip_next = False
            while i < len(sentences):
                if skip_next:
                    skip_next = False
                    i += 1
                    continue
                s = sentences[i]
                if re.search(pattern, s, re.IGNORECASE):
                    has_fix = True
                    # 跳过这个句子和它的标点
                    if i + 1 < len(sentences) and re.match(r'[。！？\n]', sentences[i + 1]):
                        skip_next = True
                    i += 1
                    continue
                new_sentences.append(s)
                i += 1

            modified = ''.join(new_sentences)
            break  # 一次只修一个

    # 2. 语气违规检测
    for pattern in _TONE_VIOLATION_PATTERNS:
        match = re.search(pattern, modified)
        if match:
            # 替换为更温和的语气
            replacements = {
                "你应该": "也许你可以",
                "你必须": "建议你可以",
                "你需要": "可以试试",
                "你一定要": "不妨试试",
                "别想太多": "允许自己有这样的想法",
                "看开点": "给自己一点时间",
                "想开点": "慢慢来，不着急",
                "别矫情": "你的感受是真实的",
                "矫情": "你的感受很重要",
            }
            for old, new in replacements.items():
                if old in modified:
                    modified = modified.replace(old, new)
                    has_fix = True

    # 3. 清理空句子
    modified = re.sub(r'\s*[。！？]\s*[。！？]', '。', modified)
    modified = modified.strip()

    # 如果修正后内容太短（<10字），说明整句都被删了，返回兜底
    if len(modified) < 10:
        modified = "我就是云英呀，你的身心陪伴朋友~有什么想聊的都可以告诉我哦"
        has_fix = True

    return (not has_fix, modified)
