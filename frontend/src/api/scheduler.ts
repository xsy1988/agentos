/** 调度/自动化 API */
import { api } from "./client";
import type { TimerOut, AlarmOut } from "./types";

export interface TimerCreateIn {
  name: string;
  cron_expr: string;
  agent_id: string;
  input_template?: Record<string, unknown>;
}

export interface TimerUpdateIn {
  name?: string;
  cron_expr?: string;
  input_template?: Record<string, unknown>;
  status?: "active" | "paused";
}

export const schedulerApi = {
  // 定时任务
  timers: () => api.get<TimerOut[]>("/scheduler/timers"),
  createTimer: (body: TimerCreateIn) =>
    api.post<TimerOut>("/scheduler/timers", body),
  updateTimer: (id: string, body: TimerUpdateIn) =>
    api.patch<TimerOut>(`/scheduler/timers/${id}`, body),
  delTimer: (id: string) => api.del(`/scheduler/timers/${id}`),
  // 闹钟
  alarms: () => api.get<AlarmOut[]>("/scheduler/alarms"),
  createAlarm: (body: { content: string; fire_at: string; url?: string }) =>
    api.post<AlarmOut>("/scheduler/alarms", body),
  delAlarm: (id: string) => api.del(`/scheduler/alarms/${id}`),
};
