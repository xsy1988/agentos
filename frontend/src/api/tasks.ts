/**
 * 任务实例 API（ADR-23）：Worker 定义已文件化（见 api/workers.ts），
 * 本模块只管 L2 实例层：看板 / 主任务实例 / 子任务状态机。
 */
import { api } from "./client";
import type {
  TaskCreateOut,
  TaskDetailOut,
  TaskGroupOut,
  TaskOut,
  TaskStepOut,
} from "./types";

export interface BoardParams {
  include_empty?: boolean;
  limit_per_group?: number;
  include_closed_tasks?: boolean;
}

export const tasksApi = {
  /** 看板：按 Worker 分组 + 组内实例（名称/进度/待确认） */
  board: (params?: BoardParams) =>
    api.get<TaskGroupOut[]>("/tasks/board", params as Record<string, boolean | number>),
  list: (params?: { status?: string; worker_name?: string; limit?: number }) =>
    api.get<TaskOut[]>("/tasks", params as Record<string, string | number>),
  get: (taskId: string) => api.get<TaskDetailOut>(`/tasks/${taskId}`),
  /** 新建主任务：一把创建 会话 + 任务实例 + 步骤骨架（+ 首条 run）；
   *  worker_name = data/workers 目录名，创建时锁定其生效版本 */
  create: (body: {
    worker_name: string;
    title?: string | null;
    agent_id?: string | null;
    text?: string;
    model_provider_id?: string | null;
    // 幂等键（P1-9）：连点「新开会话并发送」/超时重试都命中同一个主任务
    client_message_id?: string | null;
  }) => api.post<TaskCreateOut>("/tasks", body),
  update: (taskId: string, body: { title?: string; status?: string }) =>
    api.patch<TaskDetailOut>(`/tasks/${taskId}`, body),
  del: (taskId: string) => api.del<void>(`/tasks/${taskId}`),
  steps: (taskId: string) => api.get<TaskStepOut[]>(`/tasks/${taskId}/steps`),
  addStep: (taskId: string, body: { name: string; description?: string; kind?: string }) =>
    api.post<TaskDetailOut>(`/tasks/${taskId}/steps`, body),
  updateStep: (
    taskId: string,
    stepId: string,
    body: { status?: string; resolution?: Record<string, unknown> | null },
  ) => api.patch<TaskDetailOut>(`/tasks/${taskId}/steps/${stepId}`, body),
};
