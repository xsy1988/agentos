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
