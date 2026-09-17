# README —— 项目导航与阅读指引

> 本文件是后续所有开发会话的**唯一入口**。不要一次性加载全部文档，按下方索引按需阅读。

## 项目一句话

个人 Agent 平台：LangGraph 驱动的 Loop 引擎为核心，通过语义发现调用注册的 MCP/工具/插件/Skill 完成任务，PostgreSQL（含 pgvector）一库多角色，前后端分离，长期运行于个人 Mac。

## 当前状态

| 项 | 状态 |
|---|---|
| 设计阶段 | ✅ 已收官（五份文档全部定稿） |
| 开发阶段 | ✅ M6 沉淀完成：skills_forge 技能沉淀闭环（skill_proposals 草稿表 + on_run_end 复盘钩子挂链尾：run done 且迭代 ≥3 异步触发三问 LLM[方法可复用吗/已有技能覆盖吗/写 SKILL.md 草稿] + 与现有 skill 语义比对 >0.9 转修订建议 + 确认驳回触发纠错沉淀）+ 审核流 API（草稿列表/diff/批准转 capabilities(type=skill) → indexer 进检索池/驳回）+ retriever 语义池纳入 skill 类型 + assembler 收集命中 SKILL.md + context_assembly 注入「可用技能」区；DoD 全项验收通过（查+决策任务→复盘产出草稿 decide-accidental-damage-protection；批准入库；新会话同类任务检索命中该 Skill 排名第一并参照其步骤回答）；后端 M1-M6 全部完成，下一步：前端（见开发计划 §3） |
| 已有代码 | core/db 会话层、主数据表 + 业务表 ORM + 迁移（含 model_usage_daily 记账）、auth、agents CRUD、models 模块（Fernet 加密 + OpenAI 兼容 Provider + embedding）、engine 七节点图（intent_router/context_assembly/planner/confirm_plan/agent/tools/verify）+ inbox worker + interrupt 确认/恢复（timer run 计划自动批准）+ 预算四闸熔断 + 重启 reconcile + SSE（seq 续传）+ 记忆注入 + 技能注入、capabilities（CRUD/冒烟/mcp_client 池/绑定）、discovery（retriever 语义 Top-K 含 skill/assembler 五区装配）、files/knowledge（上传/目录树/管道/检索/reindex）、memory（整理 Job/手改/注入）、scheduler（timers/alarms/每日整理）、notifications（通知中心）、skills_forge（复盘钩子/审核流/纠错沉淀） |
| 技术环境 | uv 0.12.5 · Python 3.12.14 · PG17+pgvector（agent-platform-db）· langgraph 1.2.11 锁版 · `make dev / migrate / upgrade / test / lint` |

> 纪律：每完成一个阶段/里程碑，更新本表；偏离设计的临时决定必须补记到设计方案 §12 ADR。

## 文档索引（按需阅读，不要全量加载）

| 文档 | 内容 | 什么时候读 |
|---|---|---|
| [docs/设计方案.md](docs/设计方案.md) | 总体架构、技术选型、16 表概览、进程模型、API v1 草案、里程碑 M1-M6、ADR | 了解全局 / 做架构级改动 / 查 API 契约 |
| [docs/模块详细设计.md](docs/模块详细设计.md) | 14 个模块的实现级设计：engine 图结构、State、inbox、hooks、预算、能力装配、记忆整理等 | **开发具体模块前必读对应章节** |
| [docs/数据库设计.md](docs/数据库设计.md) | 26 张表字段级设计（主数据 9 + 业务 14 + 框架 3）、索引、ER | 写模型/迁移/查询前必读对应表 |
| [docs/前端设计.md](docs/前端设计.md) | 菜单结构、页面交互细节、技术选型（Antd/桌面优先/工作台风） | 开发前端或前端 API 契约时 |
| [docs/开发计划.md](docs/开发计划.md) | 各阶段任务清单、DoD 验收标准、风险应对 | **每次开工前读当前阶段章节**，对照 DoD 收尾 |
| [docs/自建网页搜索服务.md](docs/自建网页搜索服务.md) | 可选的自建网页搜索栈（profile `search`）：五级质量漏斗、MCP 接入、评测基线、调参与排障 | 起停/排查网页搜索、调搜索质量、改引擎集时 |

## 任务 → 必读文档速查

| 你要做的事 | 先读 |
|---|---|
| 开始一个开发阶段 | 开发计划 §3 对应阶段（任务+DoD）→ 模块详细设计对应模块章节 |
| 建表/写迁移/改字段 | 数据库设计对应表 + 设计方案 §4（表清单上下文） |
| 开发 engine 相关 | 模块详细设计 §1.1（图/State/inbox/hooks/budget）——接口冻结以它为准 |
| 开发能力注册/发现 | 模块详细设计 §1.2 §1.3 + 数据库设计 §1.4-1.6 |
| 写 API 端点 | 设计方案 §8（API v1 草案）+ 前端设计对应页面（确认交互契约） |
| 前端开发 | 前端设计全文（菜单/页面/交互） |
| 疑问"为什么这么设计" | 设计方案 §12 ADR + §2 总体架构 |

## 硬约束速查（违反即返工）

1. **依赖白名单**：核心进程只允许设计方案 §7 列出的依赖，新增依赖须先补 ADR
2. **能力执行不在应用进程内**：一切工具经 MCP/HTTP 出进程
3. **模块间共享数据只经 PostgreSQL**（engine↔discovery/capabilities 进程内调用除外），不许私建网络通道
4. **Engine 不碰前端**：只写库和事件，SSE 由 API 服务负责
5. **上下文保护名单**：protected_context / 活跃计划 / 当前工具描述永不压缩
6. 单元测试只覆盖纯逻辑（预算闸/压缩/校验），验收以各阶段 DoD 冒烟脚本为准
7. 向量维度全局统一（部署配置，默认 1536），换 embedding 模型 = 三处向量重建 + 迁移

## 目录结构（随开发更新）

```
Agent平台/
├── README.md          ← 本文件（导航）
├── docs/              ← 设计文档（见上表）
├── docker-compose.yml ← PG17 + pgvector（+ 可选 profile:search 网页搜索栈）
├── Makefile           ← db-up / dev / test / lint / search-up 等命令
├── .env / .env.example / .pre-commit-config.yaml
├── services/          ← 容器隔离区：websearch（五级质量漏斗搜索服务，profile `search`）
└── backend/           ← uv + FastAPI（app/core/config.py、app/main.py）
```
