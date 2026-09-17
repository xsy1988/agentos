# 第三方 Worker 开发与注册规范

> **读者**：想在 Agent 平台上运行自己 Worker（任务型 Agent）的第三方开发者。
> **读完你将获得**：① 搞清 mcp / tool / plugin / skill 四类能力怎么选、怎么写；② 按三级披露
> 规范写出一套 WORKER.md 文件包；③ 通过开放注册 API 把「Worker + 其依赖的全部能力」一键注册进平台。
>
> 平台内部注册机制与运行时不变量的完整定义见《任务架构注册规范.md》（本文引用为《注册规范》）；
> 本文面向第三方开发者，自洽可读，深入细节处给出指向。

---

## 0. 三分钟全景

Agent 平台是一个任务导向的 Agent 运行平台：**任务是第一等公民，会话是任务的执行外壳**。
你要入驻的东西叫 **Worker**——一类可复用的工作目标，本质是一个任务型 Agent 的「任务定义」。
平台引擎（LangGraph 八节点循环图，含外部等待闸门 `await_gate`）负责调度执行，你的职责是：

```text
┌─────────────────────────────────────────────────────────────┐
│  你提供                                                      │
│  ① Worker 定义：WORKER.md（干什么/怎么干/遇何问题/调哪个能力）│
│  ② 能力（可执行单元）：mcp server / plugin 前端 / ……         │
│  ③（可选）外部服务本体：保持在你自己的进程/主机上运行        │
├─────────────────────────────────────────────────────────────┤
│  平台提供                                                    │
│  意图识别（用户消息自动路由到你的 Worker）· 工具装配 · 预算  │
│  熔断 · 高危确认门 · SSE 流式 · 任务看板 · 侧边栏人在环 ·    │
│  知识库沉淀（产出可回流供跨任务检索）                        │
└─────────────────────────────────────────────────────────────┘
```

**入驻三步**：

1. 按本文 §1 决策树选型，按 §2 编写你的能力（mcp server / plugin 前端等）；
2. 按 §3 编写 Worker 定义（WORKER.md 三级披露 + 子任务拆分）；
3. 按 §4 调用 `POST /api/v1/open/workers/register` 一键注册（Worker + 能力捆绑提交），
   按 §5 自检验收。

**必须遵守的三个不变量**（违反会被平台机制拒绝或产生错误行为）：

1. **一会话一主任务**：你的 Worker 的一次执行绑定唯一会话，不要设计跨会话共享状态的主任务。
2. **进度单一写入口**：进度由平台按子任务状态机确定性重算，Worker 不自行上报进度。
3. **能力归属显式声明**：Worker 用到的每个能力都写进 `worker.capabilities` 引用清单；
   未被任何 Worker 引用的能力自动落「通用任务」兜底域（对所有 Agent 可见）。

---

## 1. 能力选型：mcp / tool / plugin / skill

### 1.1 先搞清一个关键事实：`tool` 不是你的扩展点

平台能力四分类中，`type=tool` 的执行通道是**平台进程内的 `BUILTIN_TOOLS` 注册表**——
即工具的 Python 实现必须编译进平台后端代码。第三方**无法通过 API 注入新的可执行 tool**。
因此对第三方开发者：

- **想给 Agent 新的可执行工具 → 注册 `mcp`**（把工具做成 MCP Server，stdio 或 http 传输）；
- **`tool` 只用于「引用」平台内置工具**（`payload.builtin` 必须命中平台已注册键，
  开放 API 对不满足此条件的 tool 提交直接返回 422）。

当前平台内置工具键（引用时 `payload.builtin` 填这些值）：

| 内置键 | 风险级 | 用途 |
|---|---|---|
| `probe_url` | read | 依赖预检：探测服务可达性/状态码/延迟（第零步必用） |
| `search_knowledge` | read | 知识库语义检索 |
| `echo` / `dangerous_demo` | read / dangerous | 链路验证占位 |
| `run_legal_crawl` / `legal_crawl_status` | write / read | 法规爬虫管道触发与状态查询 |
| `procurement_*`（5 个） | read/write | 采购 Agent 桥接（示例案例） |

### 1.2 决策树

```text
你要提供的是什么？
│
├─ Agent 可调用的「动作」（读数据/写数据/执行操作）
│    └─ 平台没有现成内置工具 → 注册【mcp】（stdio 本地进程 或 http 远程服务）
│         · 只读操作 → risk_level=read
│         · 写操作   → risk_level=write|dangerous（每次调用过用户确认门）
│
├─ 平台已有内置工具的能力
│    └─ 只是让 Worker 引用它 → 引用即可（也可注册 type=tool 的引用条目，
│       payload.builtin=内置键，见 §2.2）
│
├─ 用户要在平台内看到的「页面/交互」（选一张表、确认一批数据、看一个面板）
│    └─ 注册【plugin】——平台中只有 plugin 携带前端页面，由任务详情右侧边栏渲染
│         · 自带完整 Web 页（任意技术栈）→ frontend.mode=iframe（推荐）
│         · 轻交互（选/删/改）→ frontend.mode=server_driven（JSON UI schema）
│         · 可同时带 transport（同 mcp），让 Agent 也能调它的后端
│
└─ 可复用的「方法论」片段（怎么写周报、怎么做比价）——不是任务本身
     └─ 注册【skill】（SKILL.md），Worker 可在 L3 引用；注意 Skill 永不被当作任务注册
```

### 1.3 四类能力对比速查

| 维度 | mcp | tool | plugin | skill |
|---|---|---|---|---|
| 定位 | 可执行工具服务 | 平台内置工具引用 | 前端页面（±后端通道） | 方法论片段 |
| 第三方可新增 | ✅（主要扩展点） | ❌（只能引用） | ✅ | ✅ |
| 执行位置 | 独立进程 / 远程 | 平台进程内 | 前端渲染 + 可选后端 | 无执行（LLM 参照） |
| 有前端页面 | ❌ | ❌ | ✅（唯一有页面的类型） | ❌ |
| payload 必填 | `transport` | `builtin`（须命中注册表） | `frontend` 或 `transport` 至少其一 | `skill_md` |
| 冒烟方式 | 连接 → list_tools → 试调用 | 按 test_info 直接调 | 同 mcp（有 transport 时） | yaml 头校验 |

> **命名规则**：能力名全局唯一，`^[a-zA-Z0-9_.-]+$`（≤128 字符）。建议带团队前缀防撞名，
> 如 `acme_legal_search`。提交前用 `GET /api/v1/open/capabilities` 查重。

---

## 2. 各类能力的编写规范

所有能力共用一组正交属性（开放注册时逐项填写）：

| 字段 | 取值 | 说明 |
|---|---|---|
| `type` | `mcp` / `tool` / `plugin` / `skill` | 功能分类（§1） |
| `category` | `external`（默认）/ `internal` / `builtin` | 第三方一律 `external` |
| `name` | `^[a-zA-Z0-9_.-]+$` | 全局唯一 |
| `description` | 一句话 | **语义检索的命中依据**，务必写清「什么场景用它能干什么」 |
| `version` | 语义化版本，默认 `0.1.0` | |
| `risk_level` | `read` / `write` / `dangerous` | **如实定级**（见 §2.5） |
| `payload` | 结构见各小节 | |
| `secret_env` | `{KEY: value}` | 敏感凭据，Fernet 加密落库**只进不出** |
| `test_info` | 冒烟用例数组 | 注册时自动执行，**通过才启用** |

### 2.1 mcp：可执行工具服务

把你的工具实现为一个标准 **MCP Server**（Model Context Protocol）。平台支持两种传输：

**stdio（本地进程，推荐同机部署）**

```json
{
  "type": "mcp",
  "name": "acme_crm",
  "description": "查询与写入 ACME CRM 的客户、商机、跟进记录（销售类任务用）",
  "risk_level": "write",
  "payload": {
    "transport": "stdio",
    "command": "/usr/local/bin/node",
    "args": ["/opt/acme/acme-crm-mcp/dist/server.js"],
    "env": {"ACME_REGION": "cn-north"}
  },
  "secret_env": {"ACME_API_KEY": "sk-..."},
  "test_info": [
    {"tool": "get_customer", "input": {"name": "示例公司"}, "expected": "示例公司"}
  ]
}
```

**http（远程服务）**

```json
{
  "type": "mcp",
  "name": "acme_crm",
  "payload": {
    "transport": "http",
    "url": "https://mcp.acme.example.com/sse",
    "env": {}
  }
}
```

规范要点：

- **工具粒度**：一个 mcp 能力是一个 Server，内含多个 tool；注册冒烟会 `list_tools`
  并把工具清单同步进平台（每个工具可独立开关）。Agent 侧暴露名自动唯一化为
  `mcp__{能力名}__{工具名}`。
- **非敏感配置进 `payload.env`，凭据只进 `secret_env`**（加密落库，出参永远剥离；
  运行时解密注入，绝不出现在日志与工具结果中）。
- **test_info 每项**：`{"tool": 工具名, "input": {...}, "expected": "结果包含的片段"}`；
  `expected` 留空只验证「能成功执行」。冒烟 30s 超时/用例，连接失败即注册失败
  （能力落库但 `enabled=false`）。
- **健康熔断**：注册启用后平台每 60s 探活，连续 3 次失败标记 `unhealthy`
  并从 Agent 上下文摘除——请保证 Server 长期存活或可被拉起。
- Server 的工具 `description` 同样参与语义检索，认真写。

### 2.2 tool：引用平台内置工具

仅当你想给引用起个别名条目、或补一段自己的描述/测试时才需要注册；否则**直接在
Worker 的 `capabilities` 引用清单里写内置工具名即可**（`probe_url`、`search_knowledge`
等已是平台注册能力）。若注册：

```json
{
  "type": "tool",
  "name": "acme_probe",
  "description": "依赖预检探测（ACME Worker 专用别名）",
  "risk_level": "read",
  "payload": {"builtin": "probe_url", "schema": {"type": "object", "properties": {}}}
}
```

`payload.builtin` **必须命中 §1.1 表中的内置键**，否则开放注册返回 422 并提示改走 mcp。

### 2.3 plugin：前端页面（+可选后端通道）

平台中**只有 plugin 携带前端页面**，唯一宿主是任务详情右侧边栏（可拖拽变宽，最大 ≤50vw）。

**模式 A：iframe（推荐外部 plugin——任意技术栈，强隔离）**

```json
{
  "type": "plugin",
  "name": "acme_review_panel",
  "description": "ACME 审核面板：批量确认待审核单据",
  "risk_level": "write",
  "payload": {
    "frontend": {
      "mode": "iframe",
      "url": "https://acme.example.com/panel",
      "allowlist_origin": "https://acme.example.com"
    }
  }
}
```

iframe 页面必须实现 **postMessage 桥接协议**（完整时序见《注册规范》§5.1）：

| 方向 | 消息 | 说明 |
|---|---|---|
| iframe → 平台 | `READY` | 就绪信号（加载完成即发） |
| 平台 → iframe | `WORKER_CONTEXT` | 初始化：`{task_id, step_id, idempotency_key, data(init_data), theme, lang}` |
| iframe → 平台 | `RESIZE` | `{width}` 宽度提示 |
| iframe → 平台 | `SUBMIT` | `{data, applied?}` 用户处理完的结构化结果 |
| iframe → 平台 | `CANCEL` | 取消 |

握手：发 `READY` → 收 `WORKER_CONTEXT` → 渲染 → 用户操作 → `SUBMIT`/`CANCEL`。
安全：平台只接受 `allowlist_origin`（缺省取 `url` 的 origin）内的消息；请给 iframe 最小
sandbox 授权。

**模式 B：server_driven（轻交互，零自定义 JS）**

```json
{
  "type": "plugin",
  "name": "acme_confirm_table",
  "description": "确认 3 条待建档记录",
  "risk_level": "read",
  "payload": {
    "frontend": {
      "mode": "server_driven",
      "schema": {
        "title": "确认供应商建档",
        "table": {
          "columns": [{"key": "name", "title": "供应商", "editable": true}],
          "rows": [{"name": "甲方"}],
          "selectable": true,
          "deletable": true
        },
        "actions": [{"key": "submit", "label": "确认", "kind": "submit", "style": "primary"}]
      }
    }
  }
}
```

平台用 AntD 白名单组件（Table/Input/InputNumber/Button）渲染，**不执行任何 plugin JS**。

**统一结构化回传契约**（两模式一致）：
`{action: "submit"|"cancel", data: <array|object>, applied?: [{capability, result}]}`
——经 `POST /runs/{id}/confirm` 回流引擎，回填子任务 `resolution` 后主线续跑。

plugin 可同时携带 `transport`（结构同 mcp），使 Agent 能直接调用其后端工具。

### 2.4 skill：方法论片段

```json
{
  "type": "skill",
  "name": "acme_bid_method",
  "description": "ACME 比价方法论：三轮筛选与权重打分",
  "risk_level": "read",
  "payload": {
    "skill_md": "---\nname: acme_bid_method\ndescription: 三轮筛选与权重打分\n---\n\n# 比价方法论\n…"
  }
}
```

`skill_md` 必须是合法 SKILL.md（`---` 包围的 yaml 头含 `name`/`description` + 正文）。
Skill 是给 Agent 参照的方法论，**不是可执行程序**——确定性流程请写成 playbook 或做成 mcp。

### 2.5 风险定级纪律（影响用户体验，务必如实）

| 级别 | 语义 | 运行时行为 |
|---|---|---|
| `read` | 无副作用 | 直接执行 |
| `write` | 有外部副作用 | 每次调用前抛**高危确认卡**，用户点确认才执行 |
| `dangerous` | 不可逆/高危操作 | 同上且 UI 强警示 |

**教训**（真实案例）：把只读的目录列举工具误标 `write`，导致 Agent 每次探索都要用户确认，
形成「确认疲劳」，任务长时间停在确认点。只读就是 `read`；宁可对真正的写操作从严。

### 2.6 外部长任务：异步桥接范式（强制）

单次工具执行有超时上限（默认 600s）。**任何可能超过 1 分钟的外部流程**（爬取、批处理、
多段流水线）必须拆成两个工具，遵循「触发即返回 + 独立状态查询」：

```text
xxx_trigger  (write)  → 后台启动任务，立即返回 task_id（进程脱离：start_new_session，
                        平台重启不杀任务；密钥只在拉起瞬间注入内存）
xxx_status   (read)   → 按 task_id 查询进度/产出
```

产出报告建议回流平台知识库（入库并向量化），供 `search_knowledge` 跨任务检索复用。

#### 2.6.1 平台持有等待（P0-4，推荐；需外部服务实现回调）

上一小节的 `xxx_status` 范式有一个固有代价：**等待期间 run 被模型自己的轮询占住**——
每次轮询都是一次 iteration + 一次工具调用 + 一段上下文，分钟级流程会吃掉可观的时长与
token 预算，且 run 一旦被重启对账收殓就前功尽弃。

平台因此提供**一等外部等待**：派发类工具只出网一次，随后**平台**持有等待状态，外部服务
完成后主动回调——模型侧**零轮询**。

**工作方式**（平台内建，无需 Worker 侧改动接口）：

1. 平台登记 `await_broker` 行（`waiting`），把回传地址注入派发工具的 args：

   ```json
   {"await_callback": {
      "await_id": "...", "url": "https://<平台>/api/v1/open/awaits/<await_id>/resolve",
      "token": "<回调专属凭据>", "idempotency_key": "<参数指纹>"}}
   ```

   > 示例见 `app/modules/engine/tools_builtin.py:_procurement_trigger`：**原样透传**给外部服务即可。

2. 派发成功后 run 进入 `waiting_external`（暂停执行段：`active_ms` 停表、`deadline_at` 顺延、
   `iterations`/`tool_calls` 不增长），可安全重启。
3. 外部服务完成后回调平台：

   ```bash
   curl -X POST "$PLATFORM/api/v1/open/awaits/$AWAIT_ID/resolve" \
     -H "X-API-Key: $OPEN_API_TOKEN" -H 'Content-Type: application/json' \
     -d '{"callback_token":"<注册响应里的 token>","idempotency_key":"<同上>","payload":{"score":92}}'
   ```

   | 返回 | 含义与处置 |
   |---|---|
   | `200 {"resumed":true}` | 首次落定，已唤醒 run；**正常路径** |
   | `200 {"resumed":false}` | 幂等重放或已被超时/撤销先落定：**不要重试派发**，这不是错误 |
   | `401` | `X-API-Key` 缺失/错误（或平台未配置 `open_api_token` → `503`） |
   | `403` | `callback_token` 与 `await_id` 不匹配（勿在日志里打印 token） |
   | `404` | 等待记录不存在（`await_id` 错） |
   | `409` | 带了 `idempotency_key` 但与登记时不一致（串号回调） |

**外部服务必须做到的三件事**

1. **按 `idempotency_key` 去重**：平台的重放边界是 `at-most-once`——派发已出网但进程在
   落库前崩溃时，重放会**再出网一次**。外部服务据此键去重才能避免重复计费/重复入库。
2. **回调失败要重试**（带同一个 `idempotency_key`）：网络抖动导致的回调丢失只能靠外部侧
   重试补齐；平台侧的超时（`deadline_at`）是**兜底**而不是主路径——超时后 run 收到的是
   结构化失败 `await_expired`，成果会丢。
3. **不依赖平台主动回调**：一期只做**拉取式**（外部服务调平台）。平台不会主动 POST 到外部
   地址（避免 SSRF 面），所以 `await_callback.url` 是你唯一要实现的通道。

**灰度与前置条件**

平台的等待模式由 `settings.await_external_enabled` 控制，**默认 `false`**：开关关闭时行为与
旧版完全一致（派发工具照常同步执行、`xxx_status` 轮询工具模型可见），因此**外部服务在实现
回调契约之前，平台侧必须保持 `false`**。开关打开后：

- 派发类工具（`app/modules/awaits/policy.py:AWAIT_DISPATCH_BUILTINS`）不再由模型侧等到结果；
- 被取代的轮询工具（同文件 `AWAIT_SUPERSEDED_BUILTINS`）**从模型可见列表中移除**
  （能力仍保留给平台/人工使用）——这是"模型不可能轮询"的硬保证。

**观测与人工干预**（用户 JWT）

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/v1/awaits?run_id=&status=` | 等待全貌（等谁、等到何时、等了多久）；**不含** `callback_token` |
| `POST` | `/api/v1/awaits/{await_id}/cancel` | 人工撤销等待；run 收到结构化 `await_cancelled` |

---

## 3. Worker 编写规范

Worker = `data/workers/<名>/` 下的一套文件包，核心是一份 **WORKER.md 三级逐层披露**
（上下文按需展开，既省 token 又保证指引权威）：

| 级别 | 内容 | 载入时机 |
|---|---|---|
| **L1** | yaml 头 `name` + `description`（一句话讲清「什么场景用它」） | 常驻上下文（意图识别、看板） |
| **L2** | 正文 playbook | Worker 激活时注入**永不压缩区** |
| **L3** | `references` 清单 + `references/*.md` | 执行到相应节点按需拉取 |

### 3.1 L2 playbook 必须讲清五件事 + 第零步

```markdown
# <Worker 名>

## 第零步：依赖预检（必做）
依赖 acme 服务（https://…），用 probe_url 探测；
不通 → 告知用户「依赖未就绪」并停止，不臆造数据、不空转主线。

## 干什么
（目标与产出）

## 怎么干
1. …（步骤顺序、输入输出、依赖能力）

## 会遇到什么问题 + 如何处理
- 缺关键信息 → ask_user 澄清（支线：澄清）
- 需用户批量确认 → request_decision 抛交互决策卡 + 侧边栏 plugin（支线：确认）
- 数据异常 → …

## 工具引用清单与能力路由表
- acme_crm (write)：查/写 CRM 数据
- search_knowledge (read)：检索历史沉淀
```

### 3.2 子任务拆分（sub_workers）

- 每个子任务一个文件夹：`sub_workers/<名>/WORKER.md`；
- **主线**（每次必然发生、有固定顺序）：`kind=main` + `seq` 排序，planner 按骨架推进；
- **支线**（特定条件才触发）：`kind=branch` + `optional=true`；由 Agent 调
  `ask_user`（文本澄清）/ `request_decision`（富交互决策卡，指向你的 plugin）/
  `declare_subtask`（非阻塞登记）触发；
- `capability_hint`（能力名数组）给该步打检索提示——在本 Worker 域内优先装配这些能力
  （软约束，不是白名单）。

### 3.3 版本纪律

- 开放注册首次提交落 `v1`；**再提交同一 Worker 时 `if_exists=new_version`** 会保留历史
  版本、生成 `vN+1` 并切换生效——历史版本只读，任务实例创建时锁定当时版本；
- L3 `references` 条目示例：
  `{"kind": "plugin", "title": "决策面板", "capability": "acme_review_panel"}`、
  `{"kind": "skill", "title": "比价方法论", "capability": "acme_bid_method"}`。

### 3.4 容量硬门（P0-2，发布前拦截）

发布版本（`POST /api/v1/workers/{name}/versions`）时平台会把 `capabilities:` 展开成工具清单并计数，
**展开后工具数 > `MAX_TOOLS_HARD`（24）直接 `422` 拒绝**：

```text
能力展开后共 31 个工具，超过硬上限 24；请先收敛 WORKER.md 的 capabilities 名单，或按需拆分 Worker
```

为什么是发布时拦：Worker 本身不知道调用方 Agent 的 `tool_budget`，**24 是"任何 Agent 都装不下"
的物理上限**（单 run 能塞进上下文窗口的工具数）。与其上线后在 run 里失败，不如在这一步拒绝。
两条入口共用同一道门，因此第三方**在注册时就会拿到错误**，不必等到上线：

| 入口 | 校验对象 | 拒绝响应 |
|---|---|---|
| `POST /api/v1/workers/{name}/versions`（平台侧发布） | 生效版本的 `capabilities:` 展开结果 | `422 能力展开后共 N 个工具，超过硬上限 24；请先收敛 WORKER.md 的 capabilities 名单，或按需拆分 Worker` |
| `POST /api/v1/open/workers/register`（一键注册） | 本次提交的能力包展开结果 | `422 Worker「X」的 capabilities 展开后共 N 个工具，超过硬上限 24；请收敛 capabilities 名单，或按需拆分 Worker` |

> 与运行期装配的分工：`tool_budget` 是"共享区名额"（放不下就按语义距离丢共享区工具），
> `MAX_TOOLS_HARD` 约束**必得集**（pinned + 本 Worker 域，永不切片）；真的超过时装配层
> 会显式失败并给出 `capability_overflow` 事件，而不是悄悄少给你几个工具。

---

## 4. 开放注册 API

### 4.1 鉴权与启用

所有 `/api/v1/open/*` 端点走静态令牌（与平台用户 JWT 体系隔离）：

```text
请求头：X-API-Key: <平台管理员发放的 OPEN_API_TOKEN>
```

- 平台未配置令牌 → 整组端点返回 **503**（接口停用）；
- 令牌错误/缺失 → **401**。

### 4.2 端点总览

| 方法与路径 | 用途 |
|---|---|
| `GET /api/v1/open/capabilities?include_disabled=` | 能力目录（查名冲突 / 引用平台已有能力） |
| `GET /api/v1/open/workers` | Worker 目录（确认命名不冲突） |
| `GET /api/v1/open/workers/{name}` | Worker 概览 + 引用清单校验 |
| `POST /api/v1/open/workers/register` | **一键注册**（Worker + 能力捆绑包） |

### 4.3 一键注册：`POST /api/v1/open/workers/register`

请求体 = `{worker, capabilities, if_exists}`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `worker` | object | ✓ | Worker 定义：`name`(≤128，禁路径分隔符/点开头)、`description`(L1，必填)、`icon`/`color`(可选)、`capabilities`(能力名引用清单)、`references`(L3 清单)、`playbook`(L2 正文；空则落脚手架模板)、`sub_workers`(数组：`name/seq/kind/optional/description/playbook/capability_hint`，`optional` 缺省按 kind 推断) |
| `capabilities` | array | | 逐项结构同 §2 各示例（`type/name/description/risk_level/payload/secret_env/test_info/version/category`）；**幂等：同名已存在则跳过，沿用平台既有配置** |
| `if_exists` | enum | 缺省 `fail` | Worker 已存在时的决策：`fail`(409) / `skip`(沿用现状) / `new_version`(保留历史、发布 vN+1) |

**注册编排**（服务端依次执行）：

1. 逐个注册能力：校验 payload → 落库(`enabled=false`) → **自动冒烟** → 通过才启用
   （+ 语义索引 + 连接池热加载）；冒烟失败 → 落库未启用，结果里如实报告；
2. **引用校验**：`worker.capabilities` 必须全部命中（平台已有 ∪ 本次提交），否则 **422**
   （提示把缺失能力加入提交，或改引 `GET /open/capabilities` 中的已有能力）；
3. Worker 文件包落盘：不存在 → `v1`；已存在 → 按 `if_exists` 决策。

**响应**（201）：

```json
{
  "worker_name": "供应商报价对比",
  "action": "created",
  "version": "v1",
  "capability_results": [
    {"name": "acme_crm", "type": "mcp", "status": "created", "enabled": true,
     "smoke_summary": "全部 2 项通过"},
    {"name": "probe_url", "type": "tool", "status": "exists", "enabled": true,
     "smoke_summary": "同名能力已存在，本次跳过（不覆盖既有配置）"}
  ],
  "missing_capabilities": [],
  "warnings": []
}
```

`capability_results[].status` 语义：`created`（新建且冒烟通过）／`smoke_failed`（新建但
冒烟未过，已落库未启用——修复服务后经平台管理端重试启用）／`exists`（已存在跳过）。

**错误码**：

| 码 | 场景 |
|---|---|
| 401 / 503 | 令牌无效 / 开放接口未启用 |
| 404 | 查询的 Worker 不存在 |
| 409 | Worker 已存在且 `if_exists=fail` |
| 422 | payload 结构非法；tool 未带合法 `payload.builtin`；引用清单有未注册能力；`secret_env` 超长等 |

### 4.4 完整示例（curl）

```bash
curl -X POST http://<平台地址>/api/v1/open/workers/register \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${OPEN_API_TOKEN}" \
  -d '{
  "if_exists": "new_version",
  "capabilities": [
    {
      "type": "mcp",
      "category": "external",
      "name": "acme_crm",
      "description": "查询与写入 ACME CRM 的客户、商机、跟进记录（销售类任务用）",
      "version": "1.0.0",
      "risk_level": "write",
      "payload": {
        "transport": "http",
        "url": "https://mcp.acme.example.com/sse",
        "env": {"ACME_REGION": "cn-north"}
      },
      "secret_env": {"ACME_API_KEY": "sk-xxxx"},
      "test_info": [{"tool": "get_customer", "input": {"name": "示例"}, "expected": "示例"}]
    },
    {
      "type": "plugin",
      "category": "external",
      "name": "acme_review_panel",
      "description": "ACME 审核面板：批量确认待审核单据",
      "risk_level": "write",
      "payload": {"frontend": {"mode": "iframe", "url": "https://acme.example.com/panel"}}
    }
  ],
  "worker": {
    "name": "ACME 客户尽调",
    "description": "对指定客户做 ACME CRM 尽调：拉取档案、核验商机、生成尽调报告（销售尽调场景用）",
    "icon": "🔍",
    "color": "#1677ff",
    "capabilities": ["acme_crm", "acme_review_panel", "probe_url", "search_knowledge"],
    "references": [
      {"kind": "plugin", "title": "审核面板", "capability": "acme_review_panel"}
    ],
    "playbook": "# ACME 客户尽调\n\n## 第零步：依赖预检（必做）\n…（五件事见 §3.1）",
    "sub_workers": [
      {"name": "拉取客户档案", "seq": 1, "kind": "main",
       "description": "从 ACME CRM 拉取客户基础档案与跟进记录",
       "capability_hint": ["acme_crm"], "playbook": "# 拉取客户档案\n调 acme_crm…"},
      {"name": "批量确认异常记录", "seq": 2, "kind": "branch",
       "description": "发现异常跟进记录时，抛决策卡由用户经审核面板批量确认",
       "capability_hint": ["acme_review_panel"], "playbook": "# 批量确认\nrequest_decision…"}
    ]
  }
}'
```

---

## 5. 注册后自检清单

- [ ] `capability_results` 中你的新能力全部 `created`（`smoke_failed` → 修服务 → 平台管理端重试启用）；
- [ ] `action`/`version` 符合预期（首次 `created/v1`；演进 `new_version/vN+1`）；
- [ ] L2 playbook 非空且覆盖「五件事 + 第零步依赖预检」（空模板会在 `warnings` 里提示）；
- [ ] 主线子任务有 `seq`、支线 `kind=branch`；需富交互的步骤 playbook 指明了
      `request_decision` + 对应 plugin；
- [ ] 端到端验证：平台内新建会话发一条命中你 Worker 的消息 → 看板出现任务卡 →
      planner 按主线骨架推进 → 支线抛卡/侧边栏处理 → 回传续跑 → 进度三处同源（看板/任务卡/事件）；
- [ ] 写操作工具触发确认门（risk_level 定级生效）；只读工具不打扰用户。

## 6. 安全红线

1. **凭据只进 `secret_env`**：加密落库只进不出；禁止把密钥写进 `payload.env`、
   playbook、description 或任何会回显的文本。
2. **风险如实定级**（§2.5）：虚标 `read` 的写操作 = 用户数据无确认门裸奔。
3. **不做路径逃逸**：Worker 名与文件路径不允许 `../`、绝对路径、点开头（平台侧有防护，提交会被 422）。
4. **长任务必须异步桥接**（§2.6）：同步阻塞会触发 run 超时熔断。
5. **产出可沉淀**：报告类产出建议回流知识库（向量化后跨任务可检索），但**涉密内容不得入库**。
6. **密钥轮换**：令牌泄露请联系平台管理员重置 `OPEN_API_TOKEN`（旧令牌立即全量失效）。
