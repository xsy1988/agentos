/**
 * SSE 事件流 Hook：fetch + ReadableStream 封装。
 * 自动重连 + seq 续传（前端设计 §5 技术选型）。
 *
 * 后端端点：GET /api/v1/runs/{run_id}/stream?after={lastSeq}
 * SSE 格式：id: {seq}
event: {type}
data: {json}


 */
import { useEffect, useRef, useCallback } from "react";
import { api } from "@/api/client";
import { useSSEStore } from "@/store/sse";
import type { RunEventOut } from "@/api/types";

const TERMINAL_STATUSES = new Set(["done", "failed", "aborted", "timeout"]);

interface SSEOptions {
  onEvent?: (event: RunEventOut) => void;
  onTerminal?: (event: RunEventOut) => void;
}

export function useSSE(runId: string | null, options?: SSEOptions) {
  const { pushEvent, setStatus, setReconnecting } = useSSEStore();
  const lastSeqRef = useRef(0);
  const abortedRef = useRef(false);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onEventRef = useRef(options?.onEvent);
  const onTerminalRef = useRef(options?.onTerminal);

  // 保持回调最新但不触发 effect 重连
  onEventRef.current = options?.onEvent;
  onTerminalRef.current = options?.onTerminal;

  const connect = useCallback(async () => {
    if (!runId || abortedRef.current) return;
    setStatus(runId, "connecting");

    const after = lastSeqRef.current;
    const res = await api.raw(`/runs/${runId}/stream?after=${after}`, {
      headers: { Accept: "text/event-stream" },
    });

    if (!res.ok || !res.body) {
      setStatus(runId, "disconnected");
      setReconnecting(true);
      // 3s 后重连
      reconnectTimerRef.current = setTimeout(() => connect(), 3000);
      return;
    }

    setStatus(runId, "live");
    setReconnecting(false);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE 以 \n\n 分隔事件
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() ?? "";

        for (const chunk of chunks) {
          if (chunk.startsWith(":")) continue; // keepalive

          const lines = chunk.split("\n");
          let seq = 0;
          let eventType = "";
          let data = "";

          for (const line of lines) {
            if (line.startsWith("id:")) {
              seq = parseInt(line.slice(3).trim(), 10);
            } else if (line.startsWith("event:")) {
              eventType = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              data += line.slice(5).trim();
            }
          }

          if (!seq) continue;
          lastSeqRef.current = seq;

          let payload: Record<string, unknown> = {};
          try {
            payload = data ? JSON.parse(data) : {};
          } catch {
            // 忽略解析失败
          }

          const event: RunEventOut = {
            id: 0,
            run_id: runId,
            seq,
            event_type: eventType,
            payload,
            created_at: new Date().toISOString(),
          };

          pushEvent(runId, event);
          onEventRef.current?.(event);

          // 终态检测
          if (
            eventType === "run_status" &&
            TERMINAL_STATUSES.has(payload.status as string)
          ) {
            setStatus(runId, "done");
            onTerminalRef.current?.(event);
            return; // 自然结束
          }
        }
      }
    } catch {
      // 网络中断
      if (!abortedRef.current) {
        setStatus(runId, "disconnected");
        setReconnecting(true);
        reconnectTimerRef.current = setTimeout(() => connect(), 3000);
      }
    } finally {
      reader.releaseLock();
    }
  }, [runId, pushEvent, setStatus, setReconnecting]);

  useEffect(() => {
    abortedRef.current = false;
    lastSeqRef.current = 0;
    connect();

    return () => {
      abortedRef.current = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
    };
  }, [runId, connect]);

  return { reconnect: connect };
}
