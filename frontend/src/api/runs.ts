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
  // answer：approved/rejected 为计划与高危工具确认；其余文本为支线子任务答复（ADR-24）
  confirm: (runId: string, answer: string) =>
    api.post<{ run_id: string; status: string; answer: string }>(`/runs/${runId}/confirm`, { answer }),
};
