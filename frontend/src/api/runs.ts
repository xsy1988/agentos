/** 任务/运行 API */
import { api } from "./client";
import type { RunOut, RunEventOut } from "./types";

export interface RunListParams {
  conversation_id?: string;
  status?: string;
  limit?: number;
}

export const runsApi = {
  list: (params?: RunListParams) =>
    api.get<RunOut[]>("/runs", params as Record<string, string | number | boolean | undefined>),
  get: (runId: string) => api.get<RunOut>(`/runs/${runId}`),
  events: (runId: string, after = 0) =>
    api.get<RunEventOut[]>(`/runs/${runId}/events`, { after }),
  abort: (runId: string) =>
    api.post<{ run_id: string; status: string; detail?: string }>(`/runs/${runId}/abort`),
  // answer：approved/rejected 为计划与高危工具确认；其余文本为支线子任务答复（ADR-24）；
  // extra.data/applied：侧边栏 plugin 前端的统一结构化回传（§3.5）；
  // extra.inputs：补输入卡提交（P1-4），只带 inputs、answer 留空也是合法恢复
  confirm: (
    runId: string,
    answer: string,
    extra?: {
      data?: unknown;
      applied?: Array<{ capability: string; result: unknown }>;
      inputs?: Record<string, string | number | boolean | null>;
    },
  ) =>
    api.post<{ run_id: string; status: string; answer: string }>(`/runs/${runId}/confirm`, {
      answer,
      ...(extra ?? {}),
    }),
};
