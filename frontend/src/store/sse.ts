/**
 * SSE 事件流状态管理：当前活跃的 run stream + 事件缓冲。
 * F2 对话页消费。
 */
import { create } from "zustand";
import type { RunEventOut } from "@/api/types";

interface SSEState {
  // run_id → 事件列表
  eventsByRun: Record<string, RunEventOut[]>;
  // run_id → 连接状态
  statusByRun: Record<string, "connecting" | "live" | "disconnected" | "done">;
  // 当前正在关注的 run（右栏详情）
  activeRunId: string | null;
  // 断线重连横幅
  reconnecting: boolean;

  pushEvent: (runId: string, event: RunEventOut) => void;
  setStatus: (runId: string, status: SSEState["statusByRun"][string]) => void;
  setActiveRun: (runId: string | null) => void;
  setReconnecting: (v: boolean) => void;
  clearRun: (runId: string) => void;
}

export const useSSEStore = create<SSEState>((set) => ({
  eventsByRun: {},
  statusByRun: {},
  activeRunId: null,
  reconnecting: false,

  pushEvent: (runId, event) =>
    set((s) => {
      const list = s.eventsByRun[runId] ?? [];
      // seq 去重：断线续传可能收到重复
      if (list.some((e) => e.seq === event.seq)) return s;
      return {
        eventsByRun: { ...s.eventsByRun, [runId]: [...list, event] },
      };
    }),

  setStatus: (runId, status) =>
    set((s) => ({
      statusByRun: { ...s.statusByRun, [runId]: status },
    })),

  setActiveRun: (runId) => set({ activeRunId: runId }),
  setReconnecting: (v) => set({ reconnecting: v }),
  clearRun: (runId) =>
    set((s) => {
      const { [runId]: _, ...rest } = s.eventsByRun;
      const { [runId]: __, ...restStatus } = s.statusByRun;
      return { eventsByRun: rest, statusByRun: restStatus };
    }),
}));
