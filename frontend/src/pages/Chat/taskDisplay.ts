/**
 * 任务/子任务展示常量与工具（看板、任务条、子任务清单共用，避免多处硬编码文案）。
 */

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
