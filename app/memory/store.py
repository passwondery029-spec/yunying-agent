"""记忆系统 — 短期记忆 + 长期记忆 + 健康快照 + 对话摘要 + 情感节点 + 关系层级

v4: 新增关系层级系统，用户与云英的关系随交互加深而升级
"""

from datetime import datetime
from pydantic import BaseModel, Field

from app.health.models import HealthMetrics, HealthEvent, UserHealthBaseline
from app.engines.health.engine import build_health_snapshot
from app.core.relationship import (
    Relationship, RelationLevel, update_relationship,
    get_level_prompt_suffix, get_level,
)
from loguru import logger


# === 数据模型 ===

class EmotionalNode(BaseModel):
    """情感节点 — 用户的关键情绪事件"""
    content: str          # 事件描述，如"上周失眠严重，半夜反复醒来"
    emotion: str          # 主情绪标签，如"焦虑""低落""压力"
    timestamp: str        # 发生时间
    follow_up_done: bool = False  # 是否已经主动关心过


class UserProfile(BaseModel):
    """用户长期画像"""
    user_id: str
    constitution: str = "未测评"
    main_concerns: list[str] = Field(default_factory=list)
    emotion_trend: str = "暂无数据"
    healing_progress: str = "暂无数据"
    last_meditation: str = "暂无记录"
    baseline: UserHealthBaseline = Field(default_factory=lambda: UserHealthBaseline(user_id="default"))
    purchased_products: list[str] = Field(default_factory=list)
    already_recommended: bool = False
    emotional_nodes: list[EmotionalNode] = Field(default_factory=list)  # 情感节点


class SessionState(BaseModel):
    """会话短期状态（内存缓存）"""
    user_id: str
    session_id: str
    summary: str = ""
    current_metrics: HealthMetrics | None = None
    active_events: list[HealthEvent] = Field(default_factory=list)
    last_interaction: datetime = Field(default_factory=datetime.now)
    turn_count: int = 0


# === 对话历史压缩配置 ===

RECENT_WINDOW = 14
SUMMARY_THRESHOLD = 28
SUMMARY_MAX_CHARS = 500


class MemoryStore:
    """持久化记忆存储

    对话历史 → SQLite (app.core.database)
    用户画像 → SQLite (app.core.database)
    当前会话状态 → 内存缓存（重启后从 SQLite 恢复）
    """

    # --- 关系层级 ---

    def __init__(self):
        # session_id -> SessionState（内存缓存）
        self._sessions: dict[str, SessionState] = {}
        # user_id -> UserProfile（内存缓存）
        self._profiles: dict[str, UserProfile] = {}
        # user_id -> latest HealthMetrics（内存缓存）
        self._latest_metrics: dict[str, HealthMetrics] = {}
        # user_id -> Relationship（内存缓存）
        self._relationships: dict[str, Relationship] = {}
        # 用户级并发锁
        self._user_locks: dict[str, any] = {}
        self._lock_creation_lock = None  # 延迟初始化

    def _get_user_lock(self, user_id: str):
        """获取用户级异步锁"""
        import asyncio
        if self._lock_creation_lock is None:
            self._lock_creation_lock = asyncio.Lock()
        if user_id not in self._user_locks:
            # 注意：在异步环境中需要在事件循环中创建锁
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    # --- 用户画像 ---

    async def get_profile(self, user_id: str) -> UserProfile:
        """获取用户画像，优先内存缓存，fallback SQLite"""
        if user_id in self._profiles:
            return self._profiles[user_id]

        # 从 SQLite 加载
        from app.core.database import get_profile as db_get_profile
        db_profile = await db_get_profile(user_id)
        if db_profile:
            profile = UserProfile(
                user_id=user_id,
                constitution=db_profile.get("constitution", "未测评"),
                main_concerns=db_profile.get("main_concerns", []),
            )
        else:
            profile = UserProfile(user_id=user_id)

        self._profiles[user_id] = profile
        return profile

    async def update_profile(self, user_id: str, **kwargs) -> UserProfile:
        """更新用户画像（内存 + SQLite 双写）"""
        profile = await self.get_profile(user_id)
        for key, value in kwargs.items():
            if hasattr(profile, key):
                setattr(profile, key, value)

        # 持久化到 SQLite
        from app.core.database import upsert_profile
        await upsert_profile(
            user_id,
            constitution=profile.constitution if profile.constitution != "未测评" else None,
            main_concerns=profile.main_concerns if profile.main_concerns else None,
        )

        self._profiles[user_id] = profile
        return profile

    # --- 会话状态 ---

    def get_session(self, session_id: str, user_id: str) -> SessionState:
        """获取会话状态（纯内存缓存，重启后重建）"""
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(
                user_id=user_id,
                session_id=session_id,
            )
        return self._sessions[session_id]

    async def add_message(self, session_id: str, role: str, content: str,
                          user_id: str, engine: str = "", intent: str = "") -> None:
        """添加消息到会话（内存 + SQLite 双写）"""
        session = self.get_session(session_id, user_id)
        session.last_interaction = datetime.now()
        session.turn_count += 1

        # 持久化到 SQLite
        from app.core.database import save_message, ensure_session
        await ensure_session(session_id, user_id)
        await save_message(session_id, user_id, role, content, engine, intent)

    async def get_history(
        self,
        session_id: str,
        user_id: str,
        limit: int = 10,
        include_summary: bool = True,
    ) -> list[dict]:
        """获取最近 N 轮对话历史（从 SQLite 读取）"""
        from app.core.database import get_recent_messages

        # 从 SQLite 读取最近的消息
        messages = await get_recent_messages(session_id, limit=limit * 2)

        # 获取会话摘要
        if include_summary:
            session = self.get_session(session_id, user_id)
            if session.summary:
                summary_msg = {
                    "role": "system",
                    "content": f"【对话背景摘要】\n{session.summary}",
                }
                messages = [summary_msg] + messages

        return messages

    # --- 健康数据 ---

    def update_metrics(self, user_id: str, metrics: HealthMetrics) -> None:
        """更新用户最新健康指标（内存缓存 + SQLite）"""
        self._latest_metrics[user_id] = metrics
        # 异步持久化由调用方处理（避免在同步方法中调用异步代码）

    async def persist_metrics(self, user_id: str, metrics: dict):
        """持久化健康指标到 SQLite"""
        from app.core.database import save_metrics
        await save_metrics(user_id, metrics)
        logger.debug(f"健康指标已持久化: user={user_id}")

    def get_metrics(self, user_id: str) -> HealthMetrics | None:
        """获取用户最新健康指标（内存缓存）"""
        return self._latest_metrics.get(user_id)

    def get_recent_metrics(self, user_id: str, limit: int = 2) -> list[dict]:
        """获取用户最近的 N 次健康指标记录（用于趋势分析）

        从数据库中读取最近的 metrics 记录，返回 dict 列表（最新在前）
        """
        import json
        records = []
        try:
            import asyncio
            from app.core.database import database

            async def _fetch():
                rows = await database.fetch_all(
                    "SELECT data FROM health_metrics WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit)
                )
                return [json.loads(row["data"]) if isinstance(row["data"], str) else row["data"] for row in rows]

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # 已在异步上下文中，使用线程安全方式
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    records = pool.submit(asyncio.run, _fetch()).result()
            else:
                records = asyncio.run(_fetch())
        except Exception as e:
            logger.warning(f"获取最近metrics失败: {e}")
            # fallback: 用内存缓存的数据构造单条
            cur = self.get_metrics(user_id)
            if cur:
                records = [cur.__dict__ if hasattr(cur, '__dict__') else dict(cur)]

        return records

    def update_events(self, session_id: str, user_id: str, events: list[HealthEvent]) -> None:
        """更新会话中的活跃健康事件"""
        session = self.get_session(session_id, user_id)
        session.active_events = events

    # --- 健康快照 ---

    def build_snapshot(self, user_id: str) -> str:
        """构建用户健康快照"""
        profile = self._profiles.get(user_id)
        if profile is None:
            profile = UserProfile(user_id=user_id)

        metrics = self.get_metrics(user_id)

        # 获取活跃事件
        events = []
        for session in self._sessions.values():
            if session.user_id == user_id and session.active_events:
                events.extend(session.active_events)

        return build_health_snapshot(
            metrics=metrics,
            baseline=profile.baseline,
            events=events if events else None,
            emotion_trend=profile.emotion_trend,
            last_meditation=profile.last_meditation,
        )

    # --- 情感节点记忆 ---

    def add_emotional_node(self, user_id: str, content: str, emotion: str):
        """记录用户的关键情绪事件"""
        profile = self._profiles.get(user_id)
        if profile is None:
            return

        # 去重：如果已有类似内容则不重复添加
        for node in profile.emotional_nodes:
            if node.content == content:
                return

        node = EmotionalNode(
            content=content,
            emotion=emotion,
            timestamp=datetime.now().isoformat(),
            follow_up_done=False,
        )
        profile.emotional_nodes.append(node)

        # 保留最近 20 个节点
        if len(profile.emotional_nodes) > 20:
            profile.emotional_nodes = profile.emotional_nodes[-20:]

        logger.info(f"情感节点已记录: user={user_id}, emotion={emotion}")

    def get_pending_follow_ups(self, user_id: str, limit: int = 2) -> list[EmotionalNode]:
        """获取尚未主动关心过的情感节点"""
        profile = self._profiles.get(user_id)
        if profile is None:
            return []

        pending = [n for n in profile.emotional_nodes if not n.follow_up_done]
        return pending[:limit]

    def mark_follow_up_done(self, user_id: str, content: str):
        """标记某个情感节点已关心过"""
        profile = self._profiles.get(user_id)
        if profile is None:
            return

        for node in profile.emotional_nodes:
            if node.content == content and not node.follow_up_done:
                node.follow_up_done = True
                break

    def build_emotional_context(self, user_id: str) -> str:
        """构建情感节点上下文，注入到 system prompt 中"""
        profile = self._profiles.get(user_id)
        if profile is None or not profile.emotional_nodes:
            return ""

        lines = ["## 用户的关键情绪记忆（你记得这些，适时自然关心）"]
        for node in profile.emotional_nodes[-5:]:  # 只取最近5个
            status = "✓已关心" if node.follow_up_done else "★未关心"
            lines.append(f"- [{status}] {node.emotion}：{node.content}（{node.timestamp[:10]}）")

        lines.append("\n提示：带★的节点是你还没主动关心过的，如果话题相关，可以自然地问一句，比如'上次说的那个失眠，最近好点了吗？'")
        return "\n".join(lines)

    # --- 关系层级 ---

    def get_relationship(self, user_id: str) -> Relationship:
        """获取用户关系状态"""
        if user_id not in self._relationships:
            self._relationships[user_id] = Relationship(user_id=user_id)
        return self._relationships[user_id]

    def update_relationship_score(self, user_id: str, user_message: str) -> tuple[Relationship, bool]:
        """更新关系积分和层级

        Returns:
            (更新后的关系, 是否刚升级)
        """
        rel = self.get_relationship(user_id)
        return update_relationship(rel, user_message)

    def get_relationship_prompt(self, user_id: str) -> str:
        """获取关系层级的 prompt 注入文本"""
        rel = self.get_relationship(user_id)
        return get_level_prompt_suffix(rel.level)


    # --- 摘要压缩 ---

    async def compress_session_if_needed(self, session_id: str, user_id: str):
        """检查并压缩会话历史摘要"""
        from app.core.database import count_user_turns

        turn_count = await count_user_turns(session_id)
        session = self.get_session(session_id, user_id)

        if turn_count > SUMMARY_THRESHOLD and not session.summary:
            # 触发摘要压缩（从SQLite读取早期消息）
            from app.core.database import get_recent_messages
            all_msgs = await get_recent_messages(session_id, limit=turn_count * 2)

            to_compress = all_msgs[:-RECENT_WINDOW]
            if to_compress:
                new_summary_parts = []
                for msg in to_compress:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role == "user":
                        new_summary_parts.append(f"用户说：{content[:80]}")
                    elif role == "assistant":
                        new_summary_parts.append(f"云英回应：{content[:40]}")

                new_summary = "\n".join(new_summary_parts)
                if len(new_summary) > SUMMARY_MAX_CHARS:
                    new_summary = new_summary[:SUMMARY_MAX_CHARS]

                session.summary = new_summary

                # 持久化摘要到 SQLite
                from app.core.database import update_session_summary
                await update_session_summary(session_id, new_summary)
                logger.info(f"会话摘要已压缩: session={session_id}, 压缩了{len(to_compress)}条消息")


# 全局单例
memory = MemoryStore()
