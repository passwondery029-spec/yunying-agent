# 云英 AI Agent — 开发进度与待办清单

> 最后更新：2026-04-24
> 版本：v0.2.0

---

## 一、项目概览

云英 AI 是一个身心健康陪伴 Agent，核心能力：
- **Health 引擎**：健康数据解读与建议（中西医学双重视角）
- **Healing 引擎**：情绪疗愈陪伴（CBT + 中医意疗7法融合）
- **Product 引擎**：合香产品推荐（体质→合香对应）
- **长期记忆系统**：从对话中提取碎片，跨会话记住用户
- **智能手串联动**：实时健康数据 + 异常事件主动关怀

---

## 二、已完成功能清单

### 2.1 核心架构

| 模块 | 文件 | 状态 | 说明 |
|------|------|------|------|
| FastAPI 入口 | `app/main.py` | ✅ | 路由注册、CORS、请求追踪、结构化日志 |
| 配置管理 | `app/config.py` | ✅ | pydantic-settings，.env 加载，extra=ignore |
| 意图路由 | `app/core/orchestrator.py` | ✅ | 关键词→引擎调度，意图枚举 |
| LLM 调用 | `app/core/llm.py` | ✅ | 三级降级链 + 重试 + 指数退避 + mock模式 |
| RAG 服务 | `app/core/rag.py` | ✅ | 统一知识检索，46组关键词映射，多轮对话检索 |
| 输出解析 | `app/core/output_parser.py` | ✅ | 解析 [text]/[meditation]/[product]/[action] 块 |
| 数据库层 | `app/core/database.py` | ✅ | aiosqlite，3张表(users/messages/memory_fragments) |
| 认证系统 | `app/core/auth.py` + `app/api/routes/auth.py` | ✅ | JWT + bcrypt，注册/登录/刷新/me |

### 2.2 三大引擎

| 引擎 | 提示词 | 状态 | 关键改进 |
|------|--------|------|----------|
| Health | `app/engines/health/prompts.py` | ✅ | 形神合一中西双重视角，6项×3列数据解读表，[action]标记 |
| Healing | `app/engines/healing/prompts.py` | ✅ | CBT+意疗7法融合，8种情绪×意疗映射，气功调心3法 |
| Product | `app/engines/product/prompts.py` | ✅ | RAG知识库加载，11种体质→合香对应表，中医思路说明 |

### 2.3 记忆系统

| 模块 | 文件 | 状态 | 说明 |
|------|------|------|------|
| 碎片模型 | `app/memory/fragments.py` | ✅ | 6类碎片，时间衰减(0.95^(d/7))，访问加权，矛盾推翻 |
| 碎片提取 | `app/memory/extractor.py` | ✅ | LLM提取，最近6轮输入，轻量模型，精简prompt |
| 对话存储 | `app/memory/store.py` | ✅ | SQLite持久化，28轮压缩→500字摘要，用户级并发锁 |
| 体质识别 | `app/memory/extractor.py` | ✅ | 3策略：碎片标注→关键词匹配→LLM推断 |
| 记忆API | `app/api/routes/memory.py` | ✅ | extract/fragments/search/cleanup/stats 5个端点 |

### 2.4 知识库

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `data/knowledge/tcm-psychology.md` | 1047 | ✅ | 精炼版，覆盖6章核心：形神合一、心主神明、五脏神志、情志致病、意疗7法、9种体质 |
| `data/knowledge/tcm-psychology-full-ocr.md` | ~3000 | 参考 | 完整OCR原文，作为知识源备查 |

### 2.5 前端

| 文件 | 状态 | 说明 |
|------|------|------|
| `static/index.html` | ✅ | 登录/注册页、聊天界面、blocks结构化渲染(冥想卡片/产品卡片/操作按钮) |

### 2.6 P0 级安全稳定性（全部完成）

| # | 问题 | 修复方案 | 文件 |
|---|------|----------|------|
| 1 | 无认证 | JWT + bcrypt | `app/core/auth.py`, `app/api/routes/auth.py` |
| 2 | API Key 泄露 | 日志过滤 + 降级回复模板 | `app/core/llm.py` |
| 3 | 数据内存存储 | aiosqlite 持久化 | `app/core/database.py`, `app/memory/store.py` |
| 4 | 并发竞态 | 用户级 asyncio.Lock | `app/api/routes/chat.py` |
| 5 | LLM 无重试 | 3次重试 + 指数退避 | `app/core/llm.py` |
| 6 | 日志不可追踪 | loguru 结构化日志 | `app/main.py`, 全局 |

### 2.7 P1 级体验关键（全部完成）

| # | 问题 | 修复方案 | 状态 |
|---|------|----------|------|
| 7 | 前端不渲染结构化内容 | blocks 4种类型渲染 | ✅ |
| 8 | 体质未自动识别 | 3策略自动识别+画像更新 | ✅ |
| 9 | 无主动关怀能力 | CareEngine + 3种关怀类型 | ✅ 代码已写，**未实际测试** |
| 10 | 无 WebSocket | ws端点 + ConnectionManager | ✅ 代码已写，**未实际测试** |
| 11 | 无模型降级策略 | 三级降级链 | ✅ |
| 12 | 记忆提取延迟高 | 轻量模型+精简prompt+限制输入 | ✅ |
| - | 异步任务被GC回收 | _background_tasks全局集合 | ✅ |

---

## 三、未完成板块详情

### 3.1 WebSocket + 主动关怀 — 需要实际测试验证

**已写代码但未测试**，是当前最大的"已写未验"风险。

| 文件 | 说明 |
|------|------|
| `app/core/websocket.py` | ConnectionManager，按 user_id 管理连接池，主动推送 |
| `app/core/care_engine.py` | CareEngine，基于健康事件生成关怀消息(immediate/gentle/periodic) |
| `app/api/routes/ws.py` | WebSocket 端点 /ws/{token}，支持 chat/health_data/care 消息类型 |

**待验证项**：
1. WebSocket 连接能否正常建立和保持
2. 心跳机制是否正常
3. 主动关怀消息能否推送成功
4. 断线重连是否工作
5. CareEngine 与健康事件的集成链路
6. 前端 `index.html` 是否有 WebSocket 客户端代码（**目前没有**，需要补）

**验证步骤建议**：
```bash
# 1. 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 2. 用 wscat 或 Python websockets 测试连接
pip install websockets
python -c "
import asyncio, websockets, json
async def test():
    async with websockets.connect('ws://localhost:8000/ws/test_token') as ws:
        await ws.send(json.dumps({'type': 'chat', 'content': '你好'}))
        r = await ws.recv()
        print(r)
asyncio.run(test())
"

# 3. 检查日志
tail -n 50 data/logs/yunying_*.log
```

### 3.2 记忆提取异步触发可靠性

**问题**：虽然已修复 GC 回收问题（`_background_tasks` 全局集合），但异步任务在请求返回后才执行，FastAPI 的生命周期是否保证任务完成尚不确定。

**具体风险**：
- 高并发时 asyncio task 可能排队太久
- 服务重启时未完成的 task 会丢失
- 如果提取模型慢（30s+），用户可能感知不到效果

**建议后续**：
- 考虑改为 Celery / arq 等任务队列（长期方案）
- 短期可加日志确认 task 执行率：统计触发次数 vs 完成次数

### 3.3 前端 WebSocket 客户端

`static/index.html` 目前只有 HTTP REST 对话，**没有 WebSocket 客户端代码**。

**需要补的前端功能**：
1. WebSocket 连接管理（连接/断线重连/心跳）
2. 实时消息接收与渲染
3. 主动关怀消息弹出通知
4. 健康数据实时展示

### 3.4 数据库迁移到生产级

当前用 aiosqlite（开发用），生产环境需切换到 PostgreSQL。

**待做**：
1. 将 `app/core/database.py` 的 SQLite SQL 改为 PostgreSQL 兼容
2. 或使用 SQLAlchemy ORM 统一抽象
3. Alembic 迁移脚本
4. `.env` 中已有 `DATABASE_URL=postgresql+asyncpg://...` 但代码未使用

### 3.5 CORS 安全加固

当前 `CORS_ORIGINS="*"` 允许所有来源，生产环境需限制。

**待做**：
- `.env` 配置具体域名
- `app/main.py` 中 CORS 中间件收紧

### 3.6 测试覆盖

目前几乎没有自动化测试（只有开发时的手动验证）。

**待补测试**：
| 优先级 | 模块 | 测试内容 |
|--------|------|----------|
| P0 | `app/core/auth.py` | JWT 生成/验证/过期/无效token |
| P0 | `app/api/routes/auth.py` | 注册/登录/刷新 API |
| P0 | `app/api/routes/chat.py` | 认证保护/并发控制/降级 |
| P1 | `app/memory/extractor.py` | 碎片提取/体质推断 |
| P1 | `app/core/rag.py` | 关键词匹配/多级标题 |
| P1 | `app/core/output_parser.py` | 块解析 |
| P2 | `app/core/websocket.py` | 连接管理/推送 |
| P2 | `app/core/care_engine.py` | 关怀消息生成 |

---

## 四、P2/P3 级待办（远期）

### P2 — 体验提升

| # | 问题 | 说明 |
|---|------|------|
| 1 | 对话摘要质量 | 压缩摘要目前是简单截断，可用 LLM 生成更精准摘要 |
| 2 | 多轮对话意图追踪 | 用户中途切换话题时，引擎可能没跟上 |
| 3 | 健康数据趋势分析 | 目前只做单次数据解读，缺少7天/30天趋势 |
| 4 | 产品购买链路 | 推荐了合香但没有购买跳转，需对接商城 |
| 5 | 用户反馈闭环 | 用户对建议的评价（有用/没用）没有收集 |

### P3 — 规模化

| # | 问题 | 说明 |
|---|------|------|
| 1 | 多语言支持 | 目前只有中文 |
| 2 | 语音交互 | TTS/ASR 集成 |
| 3 | 多 Agent 协作 | 不同专长 Agent 间转介 |
| 4 | 监控告警 | Prometheus metrics + Grafana |
| 5 | CI/CD | 自动化测试 + 部署流水线 |

---

## 五、关键设计决策记录

### 5.1 LLM 降级链

```
主模型(配置的llm_model) → doubao-flash → doubao-lite → 规则回复模板
     重试2次/30s          重试1次/20s      重试1次/15s
```

- `model=None`（默认）走降级链
- `model="具体模型名"` 只用该模型，不走降级链
- `llm_extractor_model` 配置后，记忆提取和体质推断优先用轻量模型

### 5.2 记忆碎片提取流程

```
每5轮触发 → 取最近12条消息 → 精简prompt(564字符) → LLM提取(≤3条)
→ 解析JSON → 写入碎片存储 → 自动更新体质画像
```

### 5.3 上下文工程 Token 预算

| 组成部分 | 占比 | 说明 |
|----------|------|------|
| System Prompt | ~30% | 引擎提示词 + 用户画像快照 |
| RAG 知识 | ~20% | 1-2话题3000字/3+话题4000字 |
| 记忆碎片 | ~5% | Top3-5条碎片 |
| 对话历史 | ~40% | 最近10轮 + 压缩摘要 |
| 输出空间 | ~5% | max_tokens 预留 |

### 5.4 用户数据隔离

- 认证：JWT token 中携带 user_id，不可伪造
- 对话：session_id = `default_{user_id}`，天然隔离
- 碎片：按 user_id 分文件存储
- 并发：每用户一把 asyncio.Lock

---

## 六、环境与启动

```bash
# 安装依赖
pip install -e ".[dev]"

# 配置 .env（必须项）
# LLM_API_KEY=你的豆包API Key
# JWT_SECRET=随机密钥（已自动生成）

# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 访问
# 前端：http://localhost:8000
# API文档：http://localhost:8000/docs
```

---

## 七、文件索引

```
app/
├── main.py                    # FastAPI 入口
├── config.py                  # 配置管理
├── api/
│   ├── schemas.py             # 请求/响应模型
│   └── routes/
│       ├── auth.py            # 认证 API
│       ├── chat.py            # 对话 API（含记忆提取触发）
│       ├── health.py          # 健康数据 API
│       ├── event.py           # 健康事件 API
│       ├── profile.py         # 用户画像 API
│       ├── memory.py          # 记忆碎片 API
│       └── ws.py              # WebSocket 端点（未测试）
├── core/
│   ├── auth.py                # JWT 认证逻辑
│   ├── llm.py                 # LLM 调用（降级链+重试）
│   ├── rag.py                 # 统一知识检索
│   ├── orchestrator.py        # 意图路由
│   ├── output_parser.py       # 结构化输出解析
│   ├── database.py            # SQLite 数据库层
│   ├── websocket.py           # WS 连接管理（未测试）
│   └── care_engine.py         # 主动关怀引擎（未测试）
├── engines/
│   ├── health/                # 健康引擎
│   ├── healing/               # 疗愈引擎
│   └── product/               # 产品引擎
├── memory/
│   ├── fragments.py           # 碎片模型+存储
│   ├── extractor.py           # 碎片提取+体质推断
│   └── store.py               # 对话存储+画像管理
├── health/
│   └── models.py              # 健康指标模型
└── rag/
    └── __init__.py            # (空)

data/
├── knowledge/
│   ├── tcm-psychology.md      # 精炼知识库（1047行）
│   └── tcm-psychology-full-ocr.md  # 完整OCR（参考）
└── memories/                  # 用户记忆碎片存储

static/
└── index.html                 # 前端页面
```
