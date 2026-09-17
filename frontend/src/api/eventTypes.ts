/**
 * 引擎事件类型的**前端单点定义**（P0-6）。
 *
 * 真源在后端 `backend/app/modules/engine/backend.py:EVENT_TYPES`；
 * `backend/tests/test_event_registry.py` 会校验本文件与该真源一致，
 * 并扫描前端不再出现未登记的事件类型分支（死分支会让维护者误判平台能力）。
 */
export const EVENT_TYPES = [
  "message_delta",
  "message_reset",
  "thought",
  "tool_call",
  "tool_result",
  "plan_updated",
  "confirmation_request",
  "budget_warning",
  "run_status",
  "error",
  "context_compacted",
  "card",
  "capability_overflow",
  "await_started",
  "await_resolved",
  "await_expired",
  "blocked",
  "unblocked",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

/** 执行过程事件：收纳进 ExecutionTrace 折叠面板（默认收起）。 */
export const TRACE_EVENT_TYPES: ReadonlySet<string> = new Set<EventType>([
  "thought",
  "tool_call",
  "tool_result",
  "context_compacted",
  "budget_warning",
]);

export function isEventType(value: string): value is EventType {
  return (EVENT_TYPES as readonly string[]).includes(value);
}
