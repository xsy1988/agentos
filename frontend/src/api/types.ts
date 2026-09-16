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
export interface TaskSwitchSuggestion {
  task_type_id: string;
  task_type_name: string;
  task_type_icon: string | null;
  confidence: number;
  reason: string;
}
/** 疑似新的主任务：不落消息不建 run，由用户拍板（ADR-27 软提示） */
export interface SendMessageTaskSwitch {
  kind: "task_switch_suggested";
  conversation_id: string;
  suggested_task_type: TaskSwitchSuggestion;
  pending_text: string;
  current_task_type_name: string;
}
export type SendMessageOut = SendMessageRunCreated | SendMessageTaskSwitch;

// ---- Runs ----
export interface RunOut {
  id: string;
  conversation_id: string | null;
  agent_id: string;
  trigger: string;
  status: string;
  input: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  budget: Record<string, unknown>;
  budget_used: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
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

// ---- 任务架构：主任务模板（L1 定义层） ----
/** L3 引用资源条目（子 WORKER.md / 卡片模板 / plugin 清单 / 数据契约等，按需拉取）。 */
export type WorkerReference = Record<string, unknown>;
export interface StepTemplateOut {
  id: string;
  seq: number;
  name: string;
  description: string;
  /** L2 playbook 正文（推进到该子任务时载入） */
  playbook: string;
  /** L3 引用资源（按需读取） */
  references: WorkerReference[] | null;
  kind: "main" | "branch";
  optional: boolean;
  capability_hint: string[] | null;
}
export interface StepTemplateIn {
  name: string;
  description?: string;
  playbook?: string;
  references?: WorkerReference[] | null;
  kind?: "main" | "branch";
  optional?: boolean;
  capability_hint?: string[] | null;
}
/** WORKER.md 文件内容（主任务或子任务 Worker 的权威编辑载体） */
export interface WorkerFileOut {
  path: string;
  content: string;
}
export interface TaskTypeOut {
  id: string;
  name: string;
  description: string;
  /** L2 playbook 正文（Worker 激活时随任务卡注入） */
  playbook: string;
  /** L3 引用资源（按需读取） */
  references: WorkerReference[] | null;
  /** business 业务主任务 / common 通用任务集（内建，不可删除） */
  kind: string;
  icon: string | null;
  color: string | null;
  sort_order: number;
  default_agent_id: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  steps: StepTemplateOut[];
  capability_count: number;
  task_count: number;
}
export interface TaskTypeCapabilityOut {
  capability_id: string;
  name: string;
  type: string;
  risk_level: string;
  enabled: boolean;
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
  task_type_id: string;
  task_type_name: string;
  task_type_icon: string | null;
  task_type_color: string | null;
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
export interface TaskGroupOut {
  task_type: TaskTypeOut;
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
