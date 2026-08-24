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
export interface SendMessageOut {
  conversation_id: string;
  run_id: string;
}

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
