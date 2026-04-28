"""统一 RAG 知识检索服务

替代各引擎中重复的 _load_tcm_knowledge + _extract_xxx_knowledge 代码。
提供：
- 知识库加载与缓存
- 基于对话上下文的关键词匹配检索
- 按引擎类型选择不同的默认知识范围
- Token 预算控制
"""

import os
from loguru import logger


class RAGService:
    """统一知识检索服务"""

    def __init__(self):
        self._knowledge: str = ""
        self._knowledge_lines: list[str] = []

    def _load(self) -> str:
        """加载知识库（只加载一次）"""
        if self._knowledge:
            return self._knowledge

        knowledge_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "knowledge", "tcm-psychology.md"
        )
        try:
            with open(knowledge_path, "r", encoding="utf-8") as f:
                self._knowledge = f.read()
                self._knowledge_lines = self._knowledge.split("\n")
            logger.info(f"知识库加载完成: {len(self._knowledge_lines)} 行")
        except FileNotFoundError:
            logger.warning("知识库文件未找到")
            self._knowledge = ""

        return self._knowledge

    # === 各引擎的关键词映射 ===

    # Health 引擎：关注身体症状、体质、脏腑、病因病机
    HEALTH_TOPICS = {
        "失眠|睡不着|睡不好|多梦|易醒|入睡|早醒": "不寐",
        "抑郁|心情不好|低落|郁闷|不开心|什么都不想": "郁证",
        "心慌|心悸|心跳|心率|心烦": "心悸",
        "焦虑|紧张|不安|担心|恐惧|害怕|胆怯": "卑惵",
        "生气|发火|愤怒|暴|烦|怒": "情志致病",
        "叹气|胸闷|堵|肝郁|疏肝": "五脏神志论",
        "体质|人格|五行|阴阳|阳虚|阴虚|气虚|气郁": "人格体质论",
        "五脏|心|肝|脾|肺|肾|神志|心主神明": "心主神明论",
        "冥想|打坐|静心|呼吸|意守|入静|观想": "气功疗法",
        "音乐|歌|听|旋律|五音": "中医音乐疗法",
        "养生|保健|调养|修身|清静|怡情": "中医心理养生",
        "四季|春夏秋冬|节气|秋天伤感|春困": "四季情志调养",
        "嗓子|喉咙|咽|梅核|吞咽": "梅核气",
        "哭|想哭|委屈|更年期|脏躁": "脏躁",
        "百合|恍惚|不想干|没胃口": "百合病",
        "穴位|按摩|针灸|内关|神门|涌泉": "针灸调神",
        "中药|方剂|方子|汤|酸枣仁|远志|安神": "药物疗法",
        "形神|身心|身体精神": "形神合一论",
        "放松|松弛|三线": "三线放松法",
    }

    # Healing 引擎：关注情绪、心理、意疗、冥想
    HEALING_TOPICS = {
        "抑郁|心情不好|低落|郁闷|不开心|悲伤|哭|什么都不想": "郁证",
        "失眠|睡不着|睡不好|多梦|易醒|入睡|早醒": "不寐",
        "焦虑|紧张|不安|担心|恐惧|害怕|胆怯": "卑惵",
        "心慌|心悸|心跳|心烦|烦躁|委屈": "脏躁",
        "生气|发火|愤怒|暴|怒": "情志相胜",
        "冥想|打坐|静心|呼吸|放松|意守|入静|观想": "气功疗法",
        "音乐|歌|听|旋律|五音": "中医音乐疗法",
        "体质|性格|五行|阳虚|阴虚|气虚|气郁": "人格体质论",
        "五脏|肝郁|心火|脾虚|肾虚|疏肝": "五脏神志论",
        "养生|保健|调养|修身|清静|怡情|畅神": "中医心理养生",
        "四季|春夏秋冬|节气|顺时|秋天伤感": "四季情志调养",
        "卑惵|自卑|胆怯|怯懦": "卑惵",
        "梅核|嗓子|喉咙|吞咽|堵": "梅核气",
        "百合|恍惚|不想干|没胃口|躺着烦躁": "百合病",
        "穴位|按摩|针灸|内关|神门": "针灸调神",
        "顺情|从欲|接纳|允许": "顺情从欲",
        "想太多|反复|纠结|思虑": "情志相胜",
        "放松|松弛|三线": "三线放松法",
    }

    # Product 引擎：关注药物、体质、养生方法
    PRODUCT_TOPICS = {
        "失眠|睡不着|睡不好|多梦|早醒|入睡": "不寐",
        "抑郁|心情不好|低落|郁闷|不开心": "郁证",
        "焦虑|紧张|不安|担心|恐惧|害怕": "情志相胜",
        "体质|阳虚|阴虚|气虚|气郁|痰湿": "个体心理保健",
        "养生|保健|调养|修身|顺时": "中医心理养生",
        "中药|方剂|方子|汤|酸枣仁|远志|龙眼": "药物疗法",
        "心慌|心悸|心跳|心烦|烦躁": "脏躁",
        "嗓子|喉咙|梅核|咽": "梅核气",
        "香|合香|檀香|沉香|薰衣草|玫瑰": "药物疗法",
    }

    # 各引擎没有命中时的默认知识
    DEFAULT_TOPICS = {
        "health": ["五脏神志论", "手串数据"],
        "healing": ["情志相胜", "意疗"],
        "product": ["药物疗法", "个体心理保健"],
    }

    def retrieve(
        self,
        messages: list[dict],
        engine_type: str = "health",
        max_chars: int = 3000,
    ) -> str:
        """基于对话上下文检索最相关的知识片段

        Args:
            messages: 最近几轮对话消息 [{"role": "user/assistant", "content": "..."}]
            engine_type: 引擎类型 health/healing/product
            max_chars: 最大返回字符数

        Returns:
            检索到的知识文本，可能为空字符串
        """
        self._load()
        if not self._knowledge:
            return ""

        # 选择关键词映射
        topic_map = {
            "health": self.HEALTH_TOPICS,
            "healing": self.HEALING_TOPICS,
            "product": self.PRODUCT_TOPICS,
        }
        topics = topic_map.get(engine_type, self.HEALTH_TOPICS)

        # 从最近几轮对话中提取关键词（不只是当前消息）
        combined_text = self._extract_context_text(messages)

        # 匹配话题
        matched_topics = self._match_topics(combined_text, topics)

        # 默认话题兜底
        if not matched_topics:
            matched_topics = self.DEFAULT_TOPICS.get(engine_type, ["五脏神志论"])

        # 提取章节内容
        result = self._extract_sections(matched_topics)

        # 控制长度
        if len(result) > max_chars:
            result = result[:max_chars] + "\n\n...(更多知识请参考完整知识库)"

        return result

    def _extract_context_text(self, messages: list[dict], recent_n: int = 4) -> str:
        """从最近N条消息中提取文本，用于关键词匹配

        优先级：用户消息 > 助手消息，最近的 > 早期的
        """
        recent = messages[-recent_n:] if len(messages) > recent_n else messages
        parts = []
        for msg in recent:
            content = msg.get("content", "")
            if msg.get("role") == "user":
                parts.append(content)  # 用户消息完整保留
            else:
                # 助手消息只取前50字（核心意思）
                parts.append(content[:50])
        return " ".join(parts)

    def _match_topics(self, text: str, topics: dict) -> list[str]:
        """根据文本匹配话题"""
        matched = []
        text_lower = text.lower()
        for keywords, topic in topics.items():
            if any(kw in text_lower for kw in keywords.split("|")):
                if topic not in matched:
                    matched.append(topic)
        return matched

    def _extract_sections(self, matched_topics: list[str]) -> str:
        """从知识库中提取匹配的章节内容

        支持 ##、###、#### 标题匹配
        匹配到 ### 或 #### 时，向上找到所属的 ## 父节一起提取
        """
        if not self._knowledge_lines:
            self._load()

        relevant_sections = []
        capturing = False
        current_section = []

        for line in self._knowledge_lines:
            if line.startswith("## ") or line.startswith("### ") or line.startswith("#### "):
                # 保存上一个章节
                if capturing and current_section:
                    relevant_sections.append("\n".join(current_section))
                    current_section = []

                # 检查是否匹配
                capturing = any(topic in line for topic in matched_topics)

            if capturing:
                current_section.append(line)

        # 保存最后一个章节
        if capturing and current_section:
            relevant_sections.append("\n".join(current_section))

        return "\n\n".join(relevant_sections)

    def estimate_tokens(self, text: str) -> int:
        """粗略估算中文文本的 token 数（1字 ≈ 1.5 token）"""
        return int(len(text) * 1.5)


# 全局单例
rag_service = RAGService()
