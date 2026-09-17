/**
 * 类型定义：对齐后端 Pydantic schemas。
 * 后端源码：backend/app/modules/​schemas.py（各模块 schemas 文件）
 */

// ---- Auth ----
export interface UserOut {
  id: string;
  username: string;
  display_name: string;
  avatar: string | null;
}
export interface TokenOut {
  access_token: string;
  token_type: string;
}
export interface UserInitIn {
  username: string;
  password: string;
  display_name: string;
}
export interface UserLoginIn {
  username: string;
  password: string;
}

// ---- Agents ----
export interface AgentOut {
  id: string;
  name: string;
  description: string;
  avatar: string | null;
  soul_md: string;
  identity_md: string;
  memory_md: string;
  system_prompt: string;
  model_provider_id: string | null;
  tool_budget: number;
  max_iterations: number;
  max_tokens_per_run: number | null;
  timeout_seconds: number | null;
  is_default: boolean;
  status: string;
  created_at: string;
  updated_at: string;
}

// ---- Conversations ----
export interface ConversationOut {
  id: string;
  agent_id: string;
  title: string;
  status: string;
  message_count: number;
  last_message_at: string | null;
  created_at: string;
}
export interface MessageOut {
  id: string;
  role: string;
  content: Record<string, unknown>;
  run_id: string | null;
  created_at: string;
}
/** 正常路径：run 已创建，前端转 SSE 订阅 */
export interface SendMessageRunCreated {
  kind: "run_created";
  conversation_id: string;
  run_id: string;
}
/** 疑似新的主任务：不落消息不建 run，由用户拍板（ADR-27 软提示） */
export interface TaskSwitchSuggestion {
  /** data/workers 目录名（POST /tasks 直接复用） */
  worker_name: string;
  worker_display_name: string;
  worker_icon: string | null;
  confidence: number;
  reason: string;
}
/** 疑似新的主任务：不落消息不建 run，由用户拍板（ADR-27 软提示） */
export interface SendMessageTaskSwitch {
  kind: "task_switch_suggested";
  conversation_id: string;
  suggested_worker: TaskSwitchSuggestion;
  pending_text: string;
  current_task_name: string;
}
export type SendMessageOut = SendMessageRunCreated | SendMessageTaskSwitch;

// ---- Runs ----
/** run 级失败载荷（后端 RunError，extra=allow 兼容历史行）。 */
export interface RunError {
  code: string;
  detail: string;
  retryable: boolean;
  source: string;
  phase: string | null;
  /** 熔断时透出：真实失败码 + 连续失败的连续工具名 */
  failure_code?: string;
  tools?: string[];
  [key: string]: unknown;
}

/** tool_result 事件失败段（后端 tool_outcome.as_error()）。 */
export interface ToolErrorPayload {
  code: string;
  detail: string;
  retryable: boolean;
  source: string;
}

export interface RunOut {
  id: string;
  conversation_id: string | null;
  agent_id: string;
  trigger: string;
  status: string;
  input: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: RunError | null;
  budget: Record<string, unknown>;
  budget_used: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  /** P0-1 时长账本：绝对截止时间 */
  deadline_at?: string | null;
  /** 活跃时长（不含暂停/等待） */
  active_ms?: number | null;
  /** 总存活时长（读取时计算）：用于「刚超时 / 已卡住」判断 */
  elapsed_ms?: number | null;
}
export interface RunEventOut {
  id: number;
  run_id: string;
  seq: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

// ---- Capabilities ----
export interface CapabilityOut {
  id: string;
  type: string;
  category: string;
  name: string;
  description: string;
  version: string;
  risk_level: string;
  payload: Record<string, unknown>;
  has_secret_env: boolean;
  test_info: unknown[];
  enabled: boolean;
  health_status: string;
  last_health_check_at: string | null;
  created_at: string;
  updated_at: string;
}

// ---- plugin 前端渲染统一规范（§3.5）----
/** plugin 前端清单（payload.frontend）：平台中只有 plugin 携带前端页面，由侧边栏渲染。 */
export interface FrontendManifest {
  /** iframe=外部 plugin 自带 Web 页；server_driven=平台原生渲染 JSON UI schema */
  mode: "iframe" | "server_driven";
  /** iframe：plugin 前端页地址（http(s)） */
  url?: string;
  /** iframe：sandbox 授权（最小化） */
  sandbox?: string;
  /** iframe：允许的 src origin（postMessage 目标校验） */
  allowlist_origin?: string;
  /** server_driven：JSON UI schema（字段/表格/动作） */
  schema?: Record<string, unknown>;
}
/** 侧边栏渲染描述符：interactive_decision 卡 open_sidebar 时构造，指向 plugin 前端。 */
export interface SidebarDescriptor {
  title?: string;
  /** 目标 plugin 能力 id（未内联 frontend 时据此查清单） */
  plugin_capability_id?: string;
  /** 卡片可直接内联清单（省一次能力查询） */
  frontend?: FrontendManifest;
  /** 初始化数据：待处理数据/主题/语言等（握手时经 WORKER_CONTEXT 下发） */
  init_data?: Record<string, unknown>;
  /** 宽度提示：0~0.5 占屏比例（≤半屏） */
  width_hint?: number;
  step_id?: string;
  idempotency_key?: string;
  run_id?: string;
}
/** 统一结构化回传契约（两模式一致）：→ 写入子任务 resolution → 引擎续跑。 */
export interface SidebarResult {
  action: "submit" | "cancel";
  /** 回传主体：数组或对象（选/删/改后的结果） */
  data?: unknown;
  /** 已落库的写入类 mcp 结果 */
  applied?: Array<{ capability: string; result: unknown }>;
}
export interface CapabilityToolOut {
  id: string;
  capability_id: string;
  tool_name: string;
  description: string;
  input_schema: Record<string, unknown>;
  enabled: boolean;
}
export interface CapabilitySmokeReport {
  passed: boolean;
  results: Array<{ tool: string; passed: boolean; error: string | null }>;
}

// ---- Knowledge ----
export interface FolderOut {
  id: string;
  name: string;
  parent_id: string | null;
  path: string;
  description: string | null;
  sort_order: number;
}
export interface DocOut {
  id: string;
  folder_id: string;
  title: string;
  source_file_id: string | null;
  source_type: string;
  status: string;
  error: string | null;
  chunk_count: number;
  embedding_model: string | null;
}
export interface DocDetailOut extends DocOut {
  embedded_count: number;
  parse_exists: boolean;
}
export interface ChunkOut {
  id: string;
  doc_id: string;
  seq: number;
  content: string;
  heading_path: string | null;
  token_count: number;
  embedded: boolean;
}
export interface SearchHit {
  chunk_id: string;
  doc_id: string;
  doc_title: string;
  heading_path: string;
  content: string;
  score: number;
}

// ---- Models ----
export interface ModelProviderOut {
  id: string;
  kind: string;
  name: string;
  impl: string;
  base_url: string;
  model_name: string;
  params: Record<string, unknown>;
  limits: Record<string, unknown>;
  status: string;
  has_api_key: boolean;
  created_at: string;
  updated_at: string;
}

// ---- Memory ----
export interface MemoryOut {
  id: string;
  kind: string;
  date: string | null;
  title: string;
  content: string;
  token_count: number;
  source_run_ids: string[];
}

// ---- Scheduler ----
export interface TimerOut {
  id: string;
  name: string;
  cron_expr: string;
  agent_id: string;
  input_template: Record<string, unknown>;
  next_fire_at: string | null;
  last_fire_at: string | null;
  status: string;
}
export interface AlarmOut {
  id: string;
  content: string;
  fire_at: string;
  url: string | null;
  status: string;
}

// ---- Notifications ----
export interface NotificationOut {
  id: string;
  run_id: string | null;
  kind: string;
  title: string;
  content: string;
  url: string | null;
  read: boolean;
  created_at: string;
}

// ---- Skills Forge ----
export interface ProposalOut {
  id: string;
  run_id: string;
  trigger: string;
  draft_md: string;
  similar_to_capability_id: string | null;
  status: string;
  review_note: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

// ---- Files ----
export interface FileOut {
  id: string;
  path: string;
  filename: string;
  mime: string;
  size: number;
  sha256: string;
  deduplicated: boolean;
}

// ---- 任务架构：Worker 文件包（L1 定义层，文件为唯一权威） ----
/** L3 引用资源条目（references/*.md 等，LLM 按需拉取） */
export type WorkerReference = Record<string, unknown>;
/** 子任务（sub_workers/<ref>/WORKER.md 的摘要） */
export interface SubWorkerOut {
  /** sub_workers 文件夹名（= task_steps.worker_step_ref） */
  ref: string;
  name: string;
  seq: number;
  kind: "main" | "branch";
  optional: boolean;
  description: string;
  capability_hint: string[];
}
export interface WorkerVersionOut {
  version: string;
  active: boolean;
  latest: boolean;
  created_at: string | null;
}
/** Worker 概览（列表页 + 详情页头部） */
export interface WorkerOut {
  name: string;
  description: string;
  icon: string | null;
  color: string | null;
  enabled: boolean;
  /** 实际生效版本（effective） */
  active_version: string | null;
  /** manifest 显式指定版本（null=跟随最新） */
  pinned_version: string | null;
  latest_version: string | null;
  versions: WorkerVersionOut[];
  /** 工具引用清单（能力名，与 capabilities 表比对校验） */
  capabilities: string[];
  references: WorkerReference[] | null;
  playbook: string;
  sub_workers: SubWorkerOut[];
  has_files: boolean;
}
/** 版本内文件树节点 */
export interface FileNodeOut {
  name: string;
  type: "dir" | "file";
  children: FileNodeOut[];
}
/** WORKER.md 等文件内容（只有 active 版本可写，历史版本只读） */
export interface WorkerFileOut {
  path: string;
  version: string;
  writable: boolean;
  content: string;
}
export interface VersionBuildOut {
  version: string;
  copied_from: string;
}
/** 工具引用清单校验结果（命中/缺失） */
export interface CapabilityRefOut {
  name: string;
  found: boolean;
  capability_id: string | null;
  type: string | null;
  risk_level: string | null;
  enabled: boolean | null;
}
export interface CapabilityRefsOut {
  version: string;
  references: CapabilityRefOut[];
  missing: string[];
}

// ---- 任务架构：主任务实例 / 子任务（L2 实例层） ----
export interface TaskStepOut {
  id: string;
  seq: number;
  name: string;
  description: string;
  kind: "main" | "branch";
  status: string;
  /** template | planner | agent_raised | user */
  source: string;
  resolution: { question?: string; answer?: string; at?: string } | null;
  run_id: string | null;
  raised_at: string | null;
  resolved_at: string | null;
  updated_at: string;
}
export interface TaskConversationBrief {
  id: string;
  title: string;
  status: string;
  message_count: number;
  last_message_at: string | null;
}
export interface TaskOut {
  id: string;
  /** 绑定的 Worker（data/workers 目录名；__common__ = 内建通用任务） */
  worker_name: string;
  worker_display_name: string;
  /** 创建时锁定的版本（通用任务为空串） */
  worker_version: string;
  worker_icon: string | null;
  agent_id: string;
  title: string;
  status: string;
  progress_done: number;
  progress_total: number;
  progress_percent: number;
  out_of_scope_count: number;
  conversation: TaskConversationBrief | null;
  awaiting_confirm: boolean;
  awaiting_steps_count: number;
  active_run_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface TaskDetailOut extends TaskOut {
  steps: TaskStepOut[];
  run_ids: string[];
}
/** 看板分组头：来自文件注册中心的 Worker 概览（或已删除 Worker 的降级信息） */
export interface WorkerGroupBrief {
  name: string;
  display_name: string;
  description: string;
  icon: string | null;
  enabled: boolean;
  active_version: string | null;
}
export interface TaskGroupOut {
  worker: WorkerGroupBrief;
  task_count: number;
  active_count: number;
  awaiting_confirm_count: number;
  progress_percent: number;
  tasks: TaskOut[];
}
export interface TaskCreateOut {
  task: TaskOut;
  conversation_id: string;
  run_id: string | null;
}
