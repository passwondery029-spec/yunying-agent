"""云英 AI 数据库层 — SQLite 异步持久化

表结构：
  - users: 用户账户（与auth配合）
  - sessions: 对话会话
  - messages: 对话消息
  - memory_fragments: 记忆碎片
  - user_profiles: 用户画像
  - health_metrics: 健康指标历史
"""

import json
import os
from datetime import datetime
from typing import Optional

import aiosqlite
from loguru import logger

DB_PATH = os.getenv("DB_PATH", "data/yunying.db")


async def get_db() -> aiosqlite.Connection:
    """获取数据库连接"""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """初始化数据库表结构"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            -- 用户表
            CREATE TABLE IF NOT EXISTS users (
                user_id   TEXT PRIMARY KEY,
                username  TEXT UNIQUE NOT NULL,
                password  TEXT NOT NULL,
                constitution TEXT DEFAULT '未测评',
                main_concerns TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now')),
                last_active TEXT DEFAULT (datetime('now'))
            );

            -- 会话表
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                summary    TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                last_msg_at TEXT DEFAULT (datetime('now')),
                msg_count  INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

            -- 消息表
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                role       TEXT NOT NULL,  -- 'user' / 'assistant' / 'system'
                content    TEXT NOT NULL,
                engine     TEXT DEFAULT '',
                intent     TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);

            -- 记忆碎片表
            CREATE TABLE IF NOT EXISTS memory_fragments (
                id            TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                content       TEXT NOT NULL,
                category      TEXT NOT NULL,
                tags          TEXT DEFAULT '[]',
                constitution  TEXT,
                emotion       TEXT,
                importance    REAL DEFAULT 0.5,
                access_count  INTEGER DEFAULT 0,
                last_accessed TEXT,
                decay_factor  REAL DEFAULT 1.0,
                is_valid      INTEGER DEFAULT 1,
                invalidated_by TEXT,
                source_session TEXT,
                source_time    TEXT,
                source_summary TEXT DEFAULT '',
                created_at     TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_fragments_user ON memory_fragments(user_id);
            CREATE INDEX IF NOT EXISTS idx_fragments_category ON memory_fragments(category);
            CREATE INDEX IF NOT EXISTS idx_fragments_valid ON memory_fragments(user_id, is_valid);

            -- 用户画像表
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id       TEXT PRIMARY KEY,
                constitution  TEXT DEFAULT '未测评',
                main_concerns TEXT DEFAULT '[]',
                emotion_trend TEXT DEFAULT '[]',
                baseline_json TEXT DEFAULT '{}',
                last_updated  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            -- 健康指标历史表
            CREATE TABLE IF NOT EXISTS health_metrics (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                heart_rate REAL,
                hrv        REAL,
                steps      INTEGER,
                sleep_hours REAL,
                sleep_quality REAL,
                skin_temp  REAL,
                spo2       REAL,
                stress_level TEXT,
                mood       TEXT,
                raw_data   TEXT DEFAULT '{}',
                recorded_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_metrics_user ON health_metrics(user_id);
            CREATE INDEX IF NOT EXISTS idx_metrics_time ON health_metrics(user_id, recorded_at);
        """)
        # 安全添加新列（已有则跳过）
        try:
            await db.execute("ALTER TABLE health_metrics ADD COLUMN sleep_quality REAL")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE health_metrics ADD COLUMN mood TEXT")
        except Exception:
            pass
        await db.commit()
    logger.info(f"数据库初始化完成: {DB_PATH}")


# === 消息操作 ===

async def save_message(session_id: str, user_id: str, role: str, content: str,
                       engine: str = "", intent: str = ""):
    """保存一条消息"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (session_id, user_id, role, content, engine, intent) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, role, content, engine, intent),
        )
        # 更新会话的最后消息时间和消息计数
        await db.execute(
            "UPDATE sessions SET last_msg_at = datetime('now'), msg_count = msg_count + 1 WHERE session_id = ?",
            (session_id,),
        )
        await db.commit()


async def get_recent_messages(session_id: str, limit: int = 20) -> list[dict]:
    """获取最近N条消息"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content, engine, intent, created_at FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows = await cursor.fetchall()
        # 反转为时间正序
        return [
            {"role": r["role"], "content": r["content"], "engine": r["engine"],
             "intent": r["intent"], "created_at": r["created_at"]}
            for r in reversed(rows)
        ]


async def get_messages_for_extraction(session_id: str, last_n: int = 10) -> list[dict]:
    """获取用于记忆提取的最近消息"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, last_n),
        )
        rows = await cursor.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# === 会话操作 ===

async def ensure_session(session_id: str, user_id: str):
    """确保会话存在，不存在则创建"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT session_id FROM sessions WHERE session_id = ?", (session_id,))
        if await cursor.fetchone() is None:
            await db.execute(
                "INSERT INTO sessions (session_id, user_id) VALUES (?, ?)",
                (session_id, user_id),
            )
            await db.commit()


async def update_session_summary(session_id: str, summary: str):
    """更新会话摘要"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET summary = ? WHERE session_id = ?",
            (summary, session_id),
        )
        await db.commit()


async def count_user_turns(session_id: str) -> int:
    """统计会话中用户消息数"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user'",
            (session_id,),
        )
        row = await cursor.fetchone()
        return row[0]


# === 记忆碎片操作 ===

async def save_fragment(fragment: dict):
    """保存一条记忆碎片"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO memory_fragments
            (id, user_id, content, category, tags, constitution, emotion,
             importance, access_count, last_accessed, decay_factor,
             is_valid, invalidated_by, source_session, source_time, source_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fragment["id"], fragment["user_id"], fragment["content"],
            fragment["category"], json.dumps(fragment.get("tags", []), ensure_ascii=False),
            fragment.get("constitution"), fragment.get("emotion"),
            fragment.get("importance", 0.5), fragment.get("access_count", 0),
            fragment.get("last_accessed"), fragment.get("decay_factor", 1.0),
            1 if fragment.get("is_valid", True) else 0,
            fragment.get("invalidated_by"), fragment.get("source_session"),
            fragment.get("source_time"), fragment.get("source_summary", ""),
            fragment.get("created_at", datetime.now().isoformat()),
        ))
        await db.commit()


async def get_valid_fragments(user_id: str) -> list[dict]:
    """获取用户所有有效碎片"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM memory_fragments WHERE user_id = ? AND is_valid = 1 ORDER BY importance DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_fragment(r) for r in rows]


async def get_all_fragments(user_id: str) -> list[dict]:
    """获取用户所有碎片（含已推翻的）"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM memory_fragments WHERE user_id = ? ORDER BY is_valid DESC, importance DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_fragment(r) for r in rows]


async def invalidate_fragment(fragment_id: str, invalidated_by: str):
    """将碎片标记为已推翻"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE memory_fragments SET is_valid = 0, invalidated_by = ? WHERE id = ?",
            (invalidated_by, fragment_id),
        )
        await db.commit()


async def increment_access(fragment_id: str):
    """增加碎片的访问计数"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE memory_fragments SET access_count = access_count + 1, last_accessed = datetime('now') WHERE id = ?",
            (fragment_id,),
        )
        await db.commit()


async def apply_decay(user_id: str) -> int:
    """对所有有效碎片执行时间衰减，淘汰低分碎片，返回淘汰数"""
    async with aiosqlite.connect(DB_PATH) as db:
        # 计算衰减：每过7天衰减5%
        await db.execute("""
            UPDATE memory_fragments
            SET decay_factor = decay_factor * 0.95
            WHERE user_id = ? AND is_valid = 1
              AND last_accessed < datetime('now', '-7 days')
        """, (user_id,))
        # 淘汰得分过低的碎片（安全碎片除外）
        await db.execute("""
            UPDATE memory_fragments
            SET is_valid = 0
            WHERE user_id = ? AND is_valid = 1
              AND importance * decay_factor < 0.1
              AND importance < 1.0
        """, (user_id,))
        await db.commit()
        # 返回淘汰数
        cursor = await db.execute("""
            SELECT COUNT(*) FROM memory_fragments
            WHERE user_id = ? AND is_valid = 0
        """, (user_id,))
        row = await cursor.fetchone()
        return row[0]


async def count_fragments(user_id: str) -> int:
    """统计用户有效碎片数"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM memory_fragments WHERE user_id = ? AND is_valid = 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0]


# === 用户画像操作 ===

async def get_profile(user_id: str) -> Optional[dict]:
    """获取用户画像"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "user_id": row["user_id"],
            "constitution": row["constitution"],
            "main_concerns": json.loads(row["main_concerns"]),
            "emotion_trend": json.loads(row["emotion_trend"]),
            "baseline": json.loads(row["baseline_json"]),
            "last_updated": row["last_updated"],
        }


async def upsert_profile(user_id: str, constitution: str = None,
                         main_concerns: list = None, emotion_trend: list = None,
                         baseline: dict = None):
    """更新用户画像"""
    async with aiosqlite.connect(DB_PATH) as db:
        # 先检查是否存在
        cursor = await db.execute("SELECT user_id FROM user_profiles WHERE user_id = ?", (user_id,))
        exists = await cursor.fetchone() is not None

        if exists:
            # 动态构建UPDATE
            updates = []
            params = []
            if constitution is not None:
                updates.append("constitution = ?")
                params.append(constitution)
            if main_concerns is not None:
                updates.append("main_concerns = ?")
                params.append(json.dumps(main_concerns, ensure_ascii=False))
            if emotion_trend is not None:
                updates.append("emotion_trend = ?")
                params.append(json.dumps(emotion_trend, ensure_ascii=False))
            if baseline is not None:
                updates.append("baseline_json = ?")
                params.append(json.dumps(baseline, ensure_ascii=False))
            if updates:
                updates.append("last_updated = datetime('now')")
                params.append(user_id)
                await db.execute(
                    f"UPDATE user_profiles SET {', '.join(updates)} WHERE user_id = ?",
                    params,
                )
        else:
            await db.execute("""
                INSERT INTO user_profiles (user_id, constitution, main_concerns, emotion_trend, baseline_json)
                VALUES (?, ?, ?, ?, ?)
            """, (
                user_id,
                constitution or "未测评",
                json.dumps(main_concerns or [], ensure_ascii=False),
                json.dumps(emotion_trend or [], ensure_ascii=False),
                json.dumps(baseline or {}, ensure_ascii=False),
            ))
        await db.commit()


# === 健康指标操作 ===

async def save_metrics(user_id: str, metrics: dict):
    """保存一条健康指标记录"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO health_metrics
            (user_id, heart_rate, hrv, steps, sleep_hours, sleep_quality, skin_temp, spo2, stress_level, mood, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            metrics.get("heart_rate"),
            metrics.get("hrv"),
            metrics.get("steps"),
            metrics.get("sleep_hours"),
            metrics.get("sleep_quality"),
            metrics.get("skin_temp"),
            metrics.get("spo2"),
            metrics.get("stress_level"),
            metrics.get("mood"),
            json.dumps(metrics, ensure_ascii=False),
        ))
        await db.commit()


async def get_latest_metrics(user_id: str) -> Optional[dict]:
    """获取最近一条健康指标"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM health_metrics WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "heart_rate": row["heart_rate"],
            "hrv": row["hrv"],
            "steps": row["steps"],
            "sleep_hours": row["sleep_hours"],
            "sleep_quality": row["sleep_quality"] if "sleep_quality" in row.keys() else None,
            "skin_temp": row["skin_temp"],
            "spo2": row["spo2"],
            "stress_level": row["stress_level"],
            "mood": row["mood"] if "mood" in row.keys() else None,
            "recorded_at": row["recorded_at"],
        }


async def get_metrics_history(user_id: str, hours: int = 24) -> list[dict]:
    """获取最近N小时的健康指标历史"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM health_metrics
               WHERE user_id = ? AND recorded_at >= datetime('now', ?)
               ORDER BY recorded_at ASC""",
            (user_id, f"-{hours} hours"),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# === 辅助函数 ===

def _row_to_fragment(row: aiosqlite.Row) -> dict:
    """将数据库行转为记忆碎片字典"""
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "content": row["content"],
        "category": row["category"],
        "tags": json.loads(row["tags"]),
        "constitution": row["constitution"],
        "emotion": row["emotion"],
        "importance": row["importance"],
        "access_count": row["access_count"],
        "last_accessed": row["last_accessed"],
        "decay_factor": row["decay_factor"],
        "is_valid": bool(row["is_valid"]),
        "invalidated_by": row["invalidated_by"],
        "source_session": row["source_session"],
        "source_time": row["source_time"],
        "source_summary": row["source_summary"],
        "created_at": row["created_at"],
    }
