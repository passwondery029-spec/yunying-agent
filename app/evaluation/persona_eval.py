"""Persona Style Evaluator — 云英人设风格量化评估

评估维度：
1. 语气一致性 (tone_consistency)  — 是否保持温暖、不命令、不官方
2. 共情深度 (empathy_depth)       — 是否遵循四步公式
3. 人设一致性 (persona_consistency) — 是否暴露底层模型/偏离角色
4. 回复风格 (response_style)       — 句式长度、表情符号、语气词
5. 健康专业性 (health_accuracy)    — 中医/健康建议是否合理

使用方式：
1. 从数据导出 API 拉取聊天记录
2. 运行评估脚本
3. 输出各维度分数 + 改进建议

可配合 CI/CD 在 prompt 更新后自动跑评估
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ────────────────────────── 评估规则 ──────────────────────────

# 1. 语气违规词
_COMMAND_WORDS = [
    "你必须", "你应该", "你需要", "赶紧", "马上去", "不要", "不准",
    "务必", "一定要", "禁止", "不可以",
]

# 2. 官方/机械语气标记
_ROBOTIC_PATTERNS = [
    r"根据我的知识", r"作为AI", r"我是一个", r"我无法", r"我没有",
    r"请咨询专业", r"以上建议仅供参考", r"本回答",
    r"作为一名", r"根据你的描述",
]

# 3. 底层模型泄露
_MODEL_LEAK_PATTERNS = [
    r"豆包", r"GPT", r"OpenAI", r"Claude", r"LLaMA", r"Qwen",
    r"大语言模型", r"大模型", r"语言模型", r"AI模型",
    r"VolcEngine", r"火山引擎", r"字节跳动",
]

# 4. 温暖语气标记（正面）
_WARM_MARKERS = [
    "呀~", "呢~", "哦~", "哒~", "呀，", "呢，",
    "陪你", "陪着你", "在你身边", "慢慢来", "不着急",
    "放心", "没事的", "一直在这", "随时找我",
]

# 5. 共情四步检测
_EMPATHY_STEP_PATTERNS = {
    "认可": [
        r"听起来.{0,6}(很|挺|特别|真的好)", r"能感受到", r"理解你",
        r"这种.{0,4}感觉", r"确实是", r"真的不容易",
    ],
    "理解": [
        r"就像.{1,10}一样", r"就好比", r"这种感觉.{0,4}很正常",
        r"很多人都会", r"谁都会", r"是很正常的",
    ],
    "归因": [
        r"中医说", r"可能是.{0,4}导致的", r"往往是因为",
        r"这和.{0,4}有关", r"从中医角度看", r"其实是",
    ],
    "建议": [
        r"试试", r"你可以试", r"要不要", r"帮你看", r"我给你",
        r"推荐", r"搭个小梯子",
    ],
}

# 6. 句式特征
_SHORT_SENTENCE_MAX = 15  # 短句阈值（字符数）


@dataclass
class EvalResult:
    """单条回复的评估结果"""
    message_id: str = ""
    score: float = 0.0           # 综合分 (0-100)
    tone_score: float = 0.0      # 语气一致性
    empathy_score: float = 0.0   # 共情深度
    persona_score: float = 0.0   # 人设一致性
    style_score: float = 0.0     # 回复风格
    issues: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)


def evaluate_single(text: str, intent: str = "general") -> EvalResult:
    """评估单条 AI 回复

    Args:
        text: AI 回复文本
        intent: 意图类型 (health/healing/product/general)

    Returns:
        EvalResult 评估结果
    """
    result = EvalResult()
    issues = []
    highlights = []

    # ── 1. 语气一致性 (0-25) ──
    tone_score = 25.0

    # 命令语气扣分
    command_found = [w for w in _COMMAND_WORDS if w in text]
    if command_found:
        tone_score -= len(command_found) * 5
        issues.append(f"命令语气: {', '.join(command_found)}")

    # 机械语气扣分
    robotic_found = [p for p in _ROBOTIC_PATTERNS if re.search(p, text)]
    if robotic_found:
        tone_score -= len(robotic_found) * 3
        issues.append(f"机械语气: {', '.join(robotic_found)}")

    # 温暖语气加分
    warm_found = [w for w in _WARM_MARKERS if w in text]
    if warm_found:
        tone_score = min(25, tone_score + min(len(warm_found) * 2, 5))
        highlights.append(f"温暖表达: {', '.join(warm_found[:3])}")

    tone_score = max(0, tone_score)
    result.tone_score = tone_score

    # ── 2. 共情深度 (0-25) ──
    empathy_steps_found = {}
    for step_name, patterns in _EMPATHY_STEP_PATTERNS.items():
        matches = [p for p in patterns if re.search(p, text)]
        if matches:
            empathy_steps_found[step_name] = matches

    steps_count = len(empathy_steps_found)
    if steps_count >= 3:
        empathy_score = 25.0
        highlights.append(f"共情四步覆盖{steps_count}步: {', '.join(empathy_steps_found.keys())}")
    elif steps_count == 2:
        empathy_score = 18.0
        highlights.append(f"共情覆盖2步: {', '.join(empathy_steps_found.keys())}")
    elif steps_count == 1:
        empathy_score = 10.0
        # 只给了建议没有认可/理解
        if "建议" in empathy_steps_found and "认可" not in empathy_steps_found:
            issues.append("直接给建议缺少情感认可")
    else:
        empathy_score = 3.0
        issues.append("缺少共情表达")

    result.empathy_score = empathy_score

    # ── 3. 人设一致性 (0-25) ──
    persona_score = 25.0

    # 模型泄露严重扣分
    leak_found = [p for p in _MODEL_LEAK_PATTERNS if re.search(p, text)]
    if leak_found:
        persona_score -= len(leak_found) * 10
        issues.append(f"模型泄露: {', '.join(leak_found)}")

    # 自称检测
    if "我是AI" in text or "我是人工智能" in text:
        persona_score -= 15
        issues.append("自称AI，人设崩塌")

    if persona_score < 25:
        pass  # 有问题
    else:
        highlights.append("人设保持一致")

    persona_score = max(0, persona_score)
    result.persona_score = persona_score

    # ── 4. 回复风格 (0-25) ──
    style_score = 15.0  # 基础分

    # 句子长度分布
    sentences = re.split(r'[。！？~\n]', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if sentences:
        short_ratio = sum(1 for s in sentences if len(s) <= _SHORT_SENTENCE_MAX) / len(sentences)
        if short_ratio > 0.4:
            style_score += 5
            highlights.append(f"短句占比{short_ratio:.0%}，口语化")
        elif short_ratio < 0.2:
            style_score -= 3
            issues.append("句子偏长，不够口语化")

    # 语气词
    modal_particles = len(re.findall(r'[呀呢吧哦哒嘛啊啦]', text))
    if modal_particles >= 2:
        style_score += 3
    elif modal_particles == 0:
        style_score -= 2
        issues.append("缺少语气词，偏书面")

    # 表情符号
    emojis = len(re.findall(r'[~～😉😊💕✨🌙🌿💡🧘💆🎵😴🌬️💬🆘❤️📊]', text))
    if 1 <= emojis <= 4:
        style_score += 2
    elif emojis > 6:
        style_score -= 2
        issues.append("表情符号过多")

    style_score = max(0, min(25, style_score))
    result.style_score = style_score

    # ── 综合分 ──
    result.score = tone_score + empathy_score + persona_score + style_score
    result.issues = issues
    result.highlights = highlights

    return result


def evaluate_conversation(messages: list[dict]) -> dict:
    """评估一段完整对话中的所有 AI 回复

    Args:
        messages: 消息列表 [{"role": "assistant"/"user", "content": "..."}]

    Returns:
        评估报告 dict
    """
    ai_messages = [m for m in messages if m.get("role") == "assistant"]

    if not ai_messages:
        return {"error": "没有AI回复可评估"}

    results = []
    for i, msg in enumerate(ai_messages):
        r = evaluate_single(msg.get("content", ""))
        r.message_id = f"msg_{i}"
        results.append(r)

    # 汇总
    avg_score = sum(r.score for r in results) / len(results)
    avg_tone = sum(r.tone_score for r in results) / len(results)
    avg_empathy = sum(r.empathy_score for r in results) / len(results)
    avg_persona = sum(r.persona_score for r in results) / len(results)
    avg_style = sum(r.style_score for r in results) / len(results)

    # 常见问题统计
    all_issues = []
    for r in results:
        all_issues.extend(r.issues)
    issue_counts = Counter(all_issues).most_common(10)

    # 亮点统计
    all_highlights = []
    for r in results:
        all_highlights.extend(r.highlights)
    highlight_counts = Counter(all_highlights).most_common(5)

    return {
        "summary": {
            "total_ai_messages": len(ai_messages),
            "avg_score": round(avg_score, 1),
            "avg_tone": round(avg_tone, 1),
            "avg_empathy": round(avg_empathy, 1),
            "avg_persona": round(avg_persona, 1),
            "avg_style": round(avg_style, 1),
            "grade": _score_to_grade(avg_score),
        },
        "top_issues": issue_counts,
        "top_highlights": highlight_counts,
        "per_message": [
            {
                "id": r.message_id,
                "score": round(r.score, 1),
                "tone": round(r.tone_score, 1),
                "empathy": round(r.empathy_score, 1),
                "persona": round(r.persona_score, 1),
                "style": round(r.style_score, 1),
                "issues": r.issues,
                "highlights": r.highlights,
            }
            for r in results
        ],
    }


def _score_to_grade(score: float) -> str:
    """分数转等级"""
    if score >= 90:
        return "A 优秀"
    elif score >= 75:
        return "B 良好"
    elif score >= 60:
        return "C 合格"
    elif score >= 40:
        return "D 待改进"
    else:
        return "F 不合格"


# ────────────────────────── CLI 入口 ──────────────────────────

def main():
    """CLI: 评估聊天记录 JSON 文件"""
    if len(sys.argv) < 2:
        print("用法: python -m app.evaluation.persona_eval <chat_history.json>")
        print("  或: python -m app.evaluation.persona_eval --sample")
        sys.exit(1)

    if sys.argv[1] == "--sample":
        # 内置样例评估
        sample_messages = [
            {"role": "user", "content": "我最近睡眠不太好，总是半夜醒过来"},
            {"role": "assistant", "content": "听起来你最近真的挺辛苦的，半夜反复醒真的特别耗精力。就像弦绷得太紧了，该放松的时候收不住。中医说这是「阳不入阴」，就是该静下来的时候心神还飘着。你可以试试睡前做3分钟缓慢的腹式呼吸，帮自己把思绪慢慢沉下来~"},
            {"role": "user", "content": "你是GPT吗？"},
            {"role": "assistant", "content": "我就是云英呀，一直在你身边倾听的~比起我是什么，你今天心情怎么样？"},
            {"role": "user", "content": "有什么冥想音乐推荐么"},
            {"role": "assistant", "content": "选对冥想音乐确实能让放松效果加倍哦，按照中医「音通人心」的说法，柔缓的自然白噪音，比如雨声、溪流声，或是古琴、尺八这类东方传统乐器的清缓曲目，都很适合搭配冥想~"},
            {"role": "user", "content": "我最近压力好大"},
            {"role": "assistant", "content": "你必须马上去休息！不要熬夜！请咨询专业医生。"},
        ]
        report = evaluate_conversation(sample_messages)
    else:
        filepath = Path(sys.argv[1])
        if not filepath.exists():
            print(f"文件不存在: {filepath}")
            sys.exit(1)

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 支持导出 API 的格式
        messages = data if isinstance(data, list) else data.get("messages", [])
        report = evaluate_conversation(messages)

    # 输出报告
    print("\n" + "=" * 60)
    print("  云英人设风格评估报告")
    print("=" * 60)

    s = report["summary"]
    print(f"\n📊 综合评分: {s['avg_score']}/100  ({s['grade']})")
    print(f"   语气一致性: {s['avg_tone']}/25")
    print(f"   共情深度:   {s['avg_empathy']}/25")
    print(f"   人设一致性: {s['avg_persona']}/25")
    print(f"   回复风格:   {s['avg_style']}/25")
    print(f"   评估消息数: {s['total_ai_messages']}")

    if report["top_issues"]:
        print(f"\n⚠️ 高频问题:")
        for issue, count in report["top_issues"]:
            print(f"   [{count}次] {issue}")

    if report["top_highlights"]:
        print(f"\n✨ 亮点:")
        for hl, count in report["top_highlights"]:
            print(f"   [{count}次] {hl}")

    # 逐条详情
    print(f"\n📝 逐条评估:")
    for msg in report["per_message"]:
        print(f"   {msg['id']}: {msg['score']}分 (语气{msg['tone']} 共情{msg['empathy']} 人设{msg['persona']} 风格{msg['style']})")
        if msg["issues"]:
            for i in msg["issues"]:
                print(f"      ⚠️ {i}")

    print("\n" + "=" * 60)
    return report


if __name__ == "__main__":
    main()
