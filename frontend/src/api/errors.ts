/**
 * 失败文案单一真源（P0-3）。
 *
 * 后端从 P0-3 起不再把工具失败翻译成自然语言"结果"，而是发结构化载荷：
 * - run 级 error 事件：{code, detail, source, retryable, phase}
 * - tool_result 事件：{tool, ok, elapsed_ms, result, error?}，error={code, detail, retryable, source}
 *
 * 前端所有展示点（RunTimeline / MessageStream）都要经过这里，避免各页各写一套
 * `payload.message ?? payload.error ?? ""`（历史 bug：新载荷下渲染空串）。
 */
import type { RunError, ToolErrorPayload } from "./types";

/** 失败码 → 给人看的一句话（后端码表见 backend/app/modules/engine/tool_outcome.py）。 */
const CODE_TEXT: Record<string, string> = {
  // 工具失败码
  external_unavailable: "外部依赖不可用",
  external_rejected: "外部服务拒绝了请求",
  invalid_args: "工具参数不正确",
  parse_error: "外部返回无法解析",
  internal_error: "平台内部错误",
  denied_by_user: "用户拒绝执行",
  // run 级 / 引擎码
  tool_failure_loop: "连续工具失败已熔断",
  budget_exceeded: "预算耗尽",
  timeout: "执行超时",
  cancelled: "已取消",
  aborted: "已中止",
  unknown: "未知错误",
};

/** 是否该给用户"重试"入口：重试是对整条 run 重新发起，瞬态失败才值得。 */
export function isRetryable(err: RunError | ToolErrorPayload | null | undefined): boolean {
  return Boolean(err?.retryable);
}

export function codeText(code: string): string {
  return CODE_TEXT[code] ?? code;
}

/**
 * 归一化任意来源的失败载荷（run.error 对象 / error 事件 payload / tool_result.error）。
 * 兼容历史行（只有 {message} 或裸字符串），保证调用方永远拿到可渲染文本。
 */
export function describeError(raw: unknown): {
  code: string;
  title: string;
  detail: string;
  retryable: boolean;
  /** 熔断时透出的底层失败码与涉及工具 */
  failureCode: string | null;
  tools: string[];
} {
  const p = (raw ?? {}) as Partial<RunError> & { message?: string };
  const code = typeof p.code === "string" && p.code ? p.code : "unknown";
  const failureCode =
    typeof p.failure_code === "string" && p.failure_code ? p.failure_code : null;
  const tools = Array.isArray(p.tools) ? p.tools.map(String) : [];
  const detail =
    (typeof p.detail === "string" && p.detail) ||
    (typeof p.message === "string" && p.message) ||
    "";
  const title =
    failureCode && failureCode !== code
      ? `${codeText(code)}（${codeText(failureCode)}）`
      : codeText(code);
  return { code, title, detail, retryable: Boolean(p.retryable), failureCode, tools };
}

/** error 事件 → 单行文案；detail 截断，避免长栈污染消息流。 */
export function errorHeadline(raw: unknown, max = 120): string {
  const { title, detail } = describeError(raw);
  if (!detail) return title;
  const oneLine = detail.replace(/\s+/g, " ");
  return `${title}：${oneLine.length > max ? `${oneLine.slice(0, max)}…` : oneLine}`;
}
