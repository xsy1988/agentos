/**
 * 任务/子任务展示常量与工具（看板、任务条、子任务清单共用，避免多处硬编码文案）。
 */
import type { TaskOut } from "@/api/types";

/**
 * 会话任务五态（需求 2/4/5）：由后端确定性字段纯前端派生，无需新增接口。
 * - awaiting（待决策）：有 paused_awaiting_confirm 的 run 或 awaiting_user 子任务
 * - running（执行中）：有未终态 run 在跑（LLM 正在调度工具/思考/输出）
 * - done（已完成）：任务完成；通用任务无活跃 run 即视为完成
 * - terminated（已终止）：任务被中途终止（failed/cancelled）
 * - waiting（等待中）：静止、未完成、无待决策（仅业务任务）
 */
export type TaskState = "done" | "running" | "waiting" | "awaiting" | "terminated";

/** 通用任务判定：与后端 COMMON_WORKER 一致（worker_name 为空串或 __common__）。 */
function isCommonTask(workerName: string): boolean {
  return workerName === "" || workerName === "__common__";
}

export function deriveTaskState(t: TaskOut): TaskState {
  if (t.awaiting_confirm) return "awaiting"; // 待决策：通用/业务任务都兜底
  if (t.active_run_id) return "running"; // 执行中：有未终态 run
  // 通用任务两态：无活跃 run 即已完成（不设 等待中/已终止）
  if (isCommonTask(t.worker_name)) return "done";
  if (t.status === "done") return "done";
  if (t.status === "failed" || t.status === "cancelled") return "terminated";
  return "waiting"; // 等待中：静止、未完成、无待决策
}

export const TASK_STATE_LABELS: Record<TaskState, string> = {
  done: "已完成",
  running: "执行中",
  waiting: "等待中",
  awaiting: "待决策",
  terminated: "已终止",
};

export const TASK_STATUS_LABELS: Record<string, string> = {
  active: "进行中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export const TASK_STATUS_COLORS: Record<string, string> = {
  active: "processing",
  done: "success",
  failed: "error",
  cancelled: "default",
};

export const STEP_STATUS_LABELS: Record<string, string> = {
  pending: "待执行",
  doing: "进行中",
  done: "已完成",
  skipped: "已跳过",
  blocked: "受阻",
  awaiting_user: "待用户确认",
};

export const STEP_STATUS_COLORS: Record<string, string> = {
  pending: "default",
  doing: "processing",
  done: "success",
  skipped: "default",
  blocked: "warning",
  awaiting_user: "warning",
};

export const STEP_SOURCE_LABELS: Record<string, string> = {
  template: "模板",
  planner: "执行中新增",
  agent_raised: "Agent 发起",
  user: "手工添加",
};

/** 受阻原因（P1-6）：枚举化展示，自由文本只做补充（resolution.detail） */
export const STEP_BLOCK_REASON_LABELS: Record<string, string> = {
  run_ended: "run 已结束",
  deadline_exceeded: "超时未答复",
  user_cancelled: "用户取消",
  manual: "人工确认",
};

/** 前台收敛动作（P1-6）：close=关闭支线；requeue=重新排队；escalate=转人工 */
export const STEP_CONVERGE_LABELS: Record<string, string> = {
  close: "关闭支线",
  requeue: "重新排队",
  escalate: "转人工",
};

/**
 * 确认卡在消息流里的一句话状态（P1-4 收尾）：`confirmation_request` 的 `reason` → 文案。
 * 五类暂停点各有各的说法，说错比不说更坏（把补输入写成"计划待确认"会让人找不到要填什么）。
 */
export const CONFIRM_LABELS: Record<string, { pending: string; done: string }> = {
  plan_review: { pending: "计划待确认", done: "计划已确认" },
  high_risk_tool: { pending: "高危操作待确认", done: "高危操作已确认" },
  subtask_clarification: { pending: "支线提问待答复", done: "支线提问已答复" },
  interactive_decision: { pending: "待你决策", done: "决策已提交" },
  input_required: { pending: "子任务待补输入", done: "子任务输入已补齐" },
};

export function stepKindLabel(kind: string): string {
  return kind === "branch" ? "支线" : "主线";
}

/** 子任务序号：支线用 B{n} 前缀，避免与主线序号混淆 */
export function stepLabel(step: { seq: number; kind: string }): string {
  return step.kind === "branch" ? `B${step.seq}` : `${step.seq}`;
}

/** 进度文案：有步骤显示 done/total，无步骤按状态兜底 */
export function progressText(t: {
  status: string;
  progress_done: number;
  progress_total: number;
}): string {
  if (t.progress_total <= 0) return t.status === "done" ? "已完成" : "未拆解步骤";
  return `${t.progress_done}/${t.progress_total}`;
}
