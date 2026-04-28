"""记忆碎片数据模型与存储层

长期记忆系统核心：从对话中提取有价值的碎片，持久化存储，按需检索注入上下文。

存储方案（P0）：JSON 文件，每用户一个文件
存储路径：data/memories/{user_id}.json
"""

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field


# ── 数据模型 ──────────────────────────────────────────────


class MemoryFragment(BaseModel):
    """一条记忆碎片"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    user_id: str

    # 核心内容
    content: str  # 碎片正文（1-3句话，精炼自包含）
    category: str  # health_fact / emotion_pattern / life_preference / relationship / healing_progress / product_intent

    # 上下文
    source_session: str = ""
    source_time: datetime = Field(default_factory=datetime.now)
    source_summary: str = ""  # 原始对话片段摘要（50字以内）

    # 标签系统
    tags: list[str] = Field(default_factory=list)
    constitution: str | None = None  # 关联体质
    emotion: str | None = None  # 关联情绪

    # 权重与衰减
    importance: float = 0.5  # 初始重要性 0-1
    access_count: int = 0  # 被检索引用次数
    last_accessed: datetime | None = None
    decay_factor: float = 1.0  # 当前衰减因子（计算得出）

    # 状态
    is_valid: bool = True
    invalidated_by: str | None = None  # 被哪条碎片推翻
    created_at: datetime = Field(default_factory=datetime.now)


class UserMemoryFile(BaseModel):
    """单个用户的记忆文件"""
    user_id: str
    constitution: str = "未测评"
    main_concerns: list[str] = Field(default_factory=list)
    fragments: list[MemoryFragment] = Field(default_factory=list)
    last_extraction: datetime | None = None
    total_extractions: int = 0


class MemoryIndex(BaseModel):
    """全局索引"""
    users: dict[str, dict] = Field(default_factory=dict)
    # user_id → {"last_update": str, "fragment_count": int}


# ── 类别常量 ──────────────────────────────────────────────

CATEGORIES = [
    "health_fact",       # 健康事实：体质/症状/病史
    "emotion_pattern",   # 情绪模式：周期性情绪/触发场景
    "life_preference",   # 生活偏好：冥想/音乐/作息偏好
    "relationship",      # 关系背景：人际困扰
    "healing_progress",  # 疗愈进展：疗效/方法掌握
    "product_intent",    # 产品关联：购买意向/已购/拒绝
]

# 类别→碎片注入时的缩写标签
CATEGORY_LABELS = {
    "health_fact": "健康",
    "emotion_pattern": "情绪",
    "life_preference": "偏好",
    "relationship": "关系",
    "healing_progress": "进展",
    "product_intent": "产品",
}

# 类别→默认重要性
CATEGORY_IMPORTANCE = {
    "health_fact": 0.9,
    "emotion_pattern": 0.7,
    "life_preference": 0.5,
    "relationship": 0.7,
    "healing_progress": 0.8,
    "product_intent": 0.6,
}


# ── 存储层 ────────────────────────────────────────────────


class MemoryFragmentStore:
    """记忆碎片的文件存储与检索"""

    def __init__(self, data_dir: str = "data/memories"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.data_dir / "_index.json"
        self._cache: dict[str, UserMemoryFile] = {}  # 内存缓存
        self._index: MemoryIndex = self._load_index()

    # ── 索引管理 ──

    def _load_index(self) -> MemoryIndex:
        if self._index_path.exists():
            try:
                data = json.loads(self._index_path.read_text("utf-8"))
                return MemoryIndex(**data)
            except Exception as e:
                logger.warning(f"加载记忆索引失败: {e}，将重建")
        return MemoryIndex()

    def _save_index(self):
        try:
            self._index_path.write_text(
                self._index.model_dump_json(indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存记忆索引失败: {e}")

    def _update_index(self, user_id: str, fragment_count: int):
        self._index.users[user_id] = {
            "last_update": datetime.now().isoformat(),
            "fragment_count": fragment_count,
        }
        self._save_index()

    # ── 文件读写 ──

    def _user_file_path(self, user_id: str) -> Path:
        return self.data_dir / f"{user_id}.json"

    def load_user_memory(self, user_id: str) -> UserMemoryFile:
        """加载用户记忆文件（带缓存）"""
        if user_id in self._cache:
            return self._cache[user_id]

        path = self._user_file_path(user_id)
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                mem = UserMemoryFile(**data)
                self._cache[user_id] = mem
                return mem
            except Exception as e:
                logger.warning(f"加载用户 {user_id} 记忆失败: {e}，将创建新文件")

        mem = UserMemoryFile(user_id=user_id)
        self._cache[user_id] = mem
        return mem

    def save_user_memory(self, mem: UserMemoryFile):
        """保存用户记忆文件"""
        path = self._user_file_path(mem.user_id)
        try:
            path.write_text(
                mem.model_dump_json(indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._cache[mem.user_id] = mem
            self._update_index(mem.user_id, len([f for f in mem.fragments if f.is_valid]))
        except Exception as e:
            logger.error(f"保存用户 {mem.user_id} 记忆失败: {e}")

    # ── 碎片写入 ──

    def add_fragments(self, user_id: str, fragments: list[MemoryFragment]):
        """添加碎片，处理冲突（新碎片推翻旧碎片）"""
        mem = self.load_user_memory(user_id)

        for frag in fragments:
            # 检查是否有冲突碎片需要失效
            if frag.invalidated_by is None:
                # 新碎片可能使旧碎片失效——通过内容相似性检测
                self._invalidate_conflicting(mem, frag)

            mem.fragments.append(frag)

            # 更新用户画像中的体质字段
            if frag.constitution and frag.constitution not in ("无", "未测评", "none", "null", ""):
                mem.constitution = frag.constitution

            # 更新用户画像中的主要困扰
            if frag.category in ("emotion_pattern", "relationship") and frag.tags:
                for tag in frag.tags:
                    if tag not in mem.main_concerns and len(mem.main_concerns) < 5:
                        mem.main_concerns.append(tag)

        # 控制碎片总量
        self._evict_if_needed(mem)

        mem.last_extraction = datetime.now()
        mem.total_extractions += 1
        self.save_user_memory(mem)

        logger.info(
            f"用户 {user_id}: 添加 {len(fragments)} 条碎片, "
            f"总有效碎片 {len([f for f in mem.fragments if f.is_valid])}"
        )

    def _invalidate_conflicting(self, mem: UserMemoryFile, new_frag: MemoryFragment):
        """检测并失效与新碎片矛盾的旧碎片"""
        for old_frag in mem.fragments:
            if not old_frag.is_valid:
                continue
            if old_frag.id == new_frag.id:
                continue  # 跳过自身
            # 同类别 + 标签重叠 → 可能是更新信息
            if old_frag.category == new_frag.category:
                overlap = set(old_frag.tags) & set(new_frag.tags)
                if overlap and len(overlap) >= 1:
                    old_frag.is_valid = False
                    old_frag.invalidated_by = new_frag.id
                    logger.debug(f"碎片 {old_frag.id} 被新碎片 {new_frag.id} 推翻")

    def _evict_if_needed(self, mem: UserMemoryFile, max_fragments: int = 100):
        """碎片超限时淘汰得分最低的"""
        valid = [f for f in mem.fragments if f.is_valid]
        if len(valid) <= max_fragments:
            return

        # 计算得分
        scored = [(self._compute_score(f), f) for f in valid]
        scored.sort(key=lambda x: x[0])

        # 淘汰得分最低的（安全相关 importance=1.0 不淘汰）
        to_evict = len(valid) - max_fragments
        evicted = 0
        for score, frag in scored:
            if evicted >= to_evict:
                break
            if frag.importance < 1.0:
                frag.is_valid = False
                evicted += 1

    # ── 碎片检索 ──

    def retrieve(
        self,
        user_id: str,
        query: str = "",
        top_k: int = 5,
        tags_filter: list[str] | None = None,
        categories_filter: list[str] | None = None,
    ) -> list[MemoryFragment]:
        """检索与当前话题最相关的记忆碎片

        Args:
            user_id: 用户ID
            query: 当前用户消息（用于关键词匹配）
            top_k: 返回最多几条
            tags_filter: 只返回包含这些标签的碎片
            categories_filter: 只返回这些类别的碎片
        """
        mem = self.load_user_memory(user_id)
        valid_frags = [f for f in mem.fragments if f.is_valid]

        if not valid_frags:
            return []

        # 1. 计算每条碎片的综合得分
        scored = []
        for frag in valid_frags:
            score = self._compute_score(frag)

            # 2. 关键词匹配加分
            if query:
                match_score = self._keyword_match_score(frag, query)
                score += match_score * 0.5  # 关键词匹配最多加0.5

            # 3. 标签过滤
            if tags_filter:
                if not set(frag.tags) & set(tags_filter):
                    continue

            # 4. 类别过滤
            if categories_filter:
                if frag.category not in categories_filter:
                    continue

            scored.append((score, frag))

        # 5. 排序取Top K
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [frag for _, frag in scored[:top_k]]

        # 6. 更新访问计数
        for frag in results:
            frag.access_count += 1
            frag.last_accessed = datetime.now()

        # 7. 保存更新后的访问信息
        if results:
            self.save_user_memory(mem)

        return results

    def _compute_score(self, frag: MemoryFragment) -> float:
        """计算碎片综合得分 = importance × 时间衰减 × 访问加权"""
        # 时间衰减
        days_since = (datetime.now() - frag.created_at).days
        decay = 0.95 ** (days_since / 7)

        # 访问加权
        access_boost = min(1 + 0.1 * frag.access_count, 2.0)

        return frag.importance * decay * access_boost

    def _keyword_match_score(self, frag: MemoryFragment, query: str) -> float:
        """关键词匹配得分"""
        query_lower = query.lower()
        match_count = 0
        total_tags = len(frag.tags) if frag.tags else 1

        # 标签匹配
        for tag in frag.tags:
            if tag in query_lower or query_lower in tag:
                match_count += 1

        # 内容关键词匹配
        content_words = set(frag.content.lower().split())
        query_words = set(query_lower.split())
        content_overlap = len(content_words & query_words)

        # 标签匹配加权 + 内容匹配
        tag_score = match_count / total_tags if total_tags > 0 else 0
        content_score = min(content_overlap / 5, 1.0) if content_overlap > 0 else 0

        return max(tag_score, content_score)

    # ── 衰减清理 ──

    def run_decay_cleanup(self, user_id: str | None = None):
        """执行衰减清理：将得分过低的碎片标记失效"""
        if user_id:
            user_ids = [user_id]
        else:
            # 清理所有用户
            user_ids = [p.stem for p in self.data_dir.glob("*.json") if p.stem != "_index"]

        for uid in user_ids:
            mem = self.load_user_memory(uid)
            changed = False
            for frag in mem.fragments:
                if not frag.is_valid:
                    continue
                score = self._compute_score(frag)
                if score < 0.1 and frag.importance < 1.0:
                    frag.is_valid = False
                    changed = True
                    logger.debug(f"碎片 {frag.id} 因衰减得分过低({score:.3f})被清理")

            if changed:
                self.save_user_memory(mem)

    # ── 格式化输出 ──

    def format_fragments_for_prompt(self, fragments: list[MemoryFragment]) -> str:
        """将碎片格式化为 system_prompt 注入文本

        格式：
        【用户记忆】
        • 阳虚体质，冬天怕冷手脚冰凉 [健康]
        • 每周一工作压力最大，周日常焦虑失眠 [情绪]
        """
        if not fragments:
            return ""

        lines = ["【用户记忆】"]
        for frag in fragments:
            label = CATEGORY_LABELS.get(frag.category, frag.category)
            lines.append(f"• {frag.content} [{label}]")

        return "\n".join(lines)

    # ── 统计 ──

    def get_stats(self, user_id: str) -> dict:
        """获取用户记忆统计"""
        mem = self.load_user_memory(user_id)
        valid = [f for f in mem.fragments if f.is_valid]
        return {
            "user_id": user_id,
            "constitution": mem.constitution,
            "main_concerns": mem.main_concerns,
            "total_fragments": len(mem.fragments),
            "valid_fragments": len(valid),
            "by_category": {
                cat: len([f for f in valid if f.category == cat])
                for cat in CATEGORIES
                if any(f.category == cat for f in valid)
            },
            "last_extraction": mem.last_extraction.isoformat() if mem.last_extraction else None,
            "total_extractions": mem.total_extractions,
        }


# ── 全局实例 ──────────────────────────────────────────────

fragment_store = MemoryFragmentStore()
