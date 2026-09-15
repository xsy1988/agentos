/**
 * 任务架构 API（ADR-23）：L1 主任务模板 + L2 主任务实例/子任务。
 * 看板数据源 = GET /tasks/board（左侧面板）。
 */
import { api } from "./client";
import type {
  StepTemplateIn,
  TaskCreateOut,
  TaskDetailOut,
  TaskGroupOut,
  TaskOut,
  TaskStepOut,
  TaskTypeCapabilityOut,
  TaskTypeOut,
} from "./types";

export interface BoardParams {
  include_empty?: boolean;
  limit_per_group?: number;
  include_closed_tasks?: boolean;
}

export const tasksApi = {
  /** 看板：按主任务分组 + 组内实例（名称/进度/待确认） */
  board: (params?: BoardParams) =>
    api.get<TaskGroupOut[]>("/tasks/board", params as Record<string, boolean | number>),
  list: (params?: { status?: string; task_type_id?: string; limit?: number }) =>
    api.get<TaskOut[]>("/tasks", params as Record<string, string | number>),
  get: (taskId: string) => api.get<TaskDetailOut>(`/tasks/${taskId}`),
  /** 新建主任务：一把创建 会话 + 任务实例 + 步骤骨架（+ 首条 run） */
  create: (body: {
    task_type_id: string;
    title?: string | null;
    agent_id?: string | null;
    text?: string;
    model_provider_id?: string | null;
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

export const taskTypesApi = {
  list: () => api.get<TaskTypeOut[]>("/task-types"),
  get: (id: string) => api.get<TaskTypeOut>(`/task-types/${id}`),
  create: (body: {
    name: string;
    description?: string;
    icon?: string | null;
    color?: string | null;
    sort_order?: number;
    default_agent_id?: string | null;
    enabled?: boolean;
    steps?: StepTemplateIn[];
  }) => api.post<TaskTypeOut>("/task-types", body),
  update: (
    id: string,
    body: Partial<{
      name: string;
      description: string;
      icon: string | null;
      color: string | null;
      sort_order: number;
      default_agent_id: string | null;
      enabled: boolean;
    }>,
  ) => api.patch<TaskTypeOut>(`/task-types/${id}`, body),
  del: (id: string) => api.del<void>(`/task-types/${id}`),
  /** 整表替换步骤模板（模板量小，避免逐条 CRUD 的排序竞态） */
  replaceSteps: (id: string, steps: StepTemplateIn[]) =>
    api.put<TaskTypeOut>(`/task-types/${id}/steps`, { steps }),
  capabilities: (id: string) =>
    api.get<TaskTypeCapabilityOut[]>(`/task-types/${id}/capabilities`),
  bindCapabilities: (id: string, capabilityIds: string[]) =>
    api.post<TaskTypeCapabilityOut[]>(`/task-types/${id}/capabilities`, {
      capability_ids: capabilityIds,
    }),
  unbindCapability: (id: string, capabilityId: string) =>
    api.del<void>(`/task-types/${id}/capabilities/${capabilityId}`),
};
