/**
 * 消息流（前端设计 §3.1 主区）。
 * 历史消息 + 实时 SSE 事件 + 流式 Agent 回复。
 */
import { useRef, useEffect, useState, Fragment } from "react";
import { useQuery } from "@tanstack/react-query";
import { Typography, Tag, Collapse, Tooltip, Space, Image, Button } from "antd";
import {
  BulbOutlined,
  ToolOutlined,
  ScheduleOutlined,
  PaperClipOutlined,
  ProfileOutlined,
  LoadingOutlined,
  DownOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { conversationsApi } from "@/api/conversations";
import { filesApi } from "@/api/files";
import { useSSEStore } from "@/store/sse";
import { useUIStore } from "@/store/ui";
import { runsApi } from "@/api/runs";
import { TRACE_EVENT_TYPES } from "@/api/eventTypes";
import { describeError } from "@/api/errors";
import MarkdownRenderer from "@/components/MarkdownRenderer";
import { fmtSize } from "@/utils/format";
import CardRenderer from "./CardRenderer";
import { STEP_BLOCK_REASON_LABELS, STEP_CONVERGE_LABELS } from "./taskDisplay";
import type { RunEventOut, MessageOut, RunOut } from "@/api/types";
import type { ActiveRun } from "./index";

// 从消息 content 提取文本
function msgText(msg: MessageOut): string {
  const c = msg.content as Record<string, unknown>;
  return (c.text as string) ?? (c.content as string) ?? "";
}

// 消息附件（后端 content.attachments：[{file_id, filename, mime, size}]）
interface MsgAttachment {
  file_id: string;
  filename: string;
  mime: string;
  size: number;
}

function msgAttachments(msg: MessageOut): MsgAttachment[] {
  const c = msg.content as Record<string, unknown>;
  const atts = c.attachments;
  return Array.isArray(atts) ? (atts as MsgAttachment[]) : [];
}

/** 图片附件：blob 鉴权拉取 → objectURL 渲染（卸载时释放）。 */
function AttachmentImage({ att }: { att: MsgAttachment }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let objectUrl: string | null = null;
    filesApi
      .fetchContent(att.file_id)
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => setFailed(true));
    return () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [att.file_id]);
  if (failed) {
    return (
      <Tooltip title="图片加载失败">
        <Tag color="error" style={{ fontSize: 11 }}>
          <PaperClipOutlined /> {att.filename}
        </Tag>
      </Tooltip>
    );
  }
  if (!url) {
    return (
      <div style={{ width: 96, height: 96, borderRadius: 6, background: "var(--ant-color-fill-quaternary)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, color: "var(--ant-color-text-tertiary)" }}>
        加载中…
      </div>
    );
  }
  return <Image src={url} alt={att.filename} width={96} height={96} style={{ objectFit: "cover", borderRadius: 6 }} />;
}

/** 用户消息附件区：图片缩略图（点击放大）+ 文档 chip。 */
function AttachmentList({ atts }: { atts: MsgAttachment[] }) {
  if (!atts.length) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
      {atts.map((att) =>
        (att.mime || "").startsWith("image/") ? (
          <AttachmentImage key={att.file_id} att={att} />
        ) : (
          <Tooltip key={att.file_id} title={`${att.mime} · ${fmtSize(att.size)}`}>
            <Tag style={{ fontSize: 11, cursor: "pointer" }}>
              <PaperClipOutlined /> {att.filename}（{fmtSize(att.size)}）
            </Tag>
          </Tooltip>
        ),
      )}
    </div>
  );
}

/** 事件流按「轮次」分段（需求 4/5）：一轮 = 模型输出文本 + 随后的工具/思考执行。
 * 边界规则：过程事件（thought/tool_*)之后出现的新文本开启新一轮；后端的
 * message_reset 是显式轮次标记（旧数据没有它也能正确分段）。
 * 最后一个无过程事件的轮的文本即终答（流式渲染主体）；其余轮文本是过程说明。 */
interface RunRound {
  text: string;
  traceEvents: RunEventOut[];
}

function splitRounds(events: RunEventOut[]): RunRound[] {
  const rounds: RunRound[] = [];
  let cur: RunRound = { text: "", traceEvents: [] };
  for (const e of events) {
    if (e.event_type === "message_delta") {
      const delta = (e.payload.text ?? e.payload.delta) as string;
      if (!delta) continue;
      // 过程事件之后的文本 = 新一轮模型输出（上一轮已随工具执行收口）
      if (cur.traceEvents.length > 0) {
        rounds.push(cur);
        cur = { text: "", traceEvents: [] };
      }
      cur.text += delta;
    } else if (e.event_type === "message_reset") {
      continue; // 显式标记：分段由「过程事件后出现新文本」规则覆盖，无需处理
    } else if (TRACE_EVENT_TYPES.has(e.event_type)) {
      cur.traceEvents.push(e);
    }
    // 其余事件（plan/confirm/status/error）由调用方单独处理
  }
  if (cur.text || cur.traceEvents.length > 0) rounds.push(cur);
  return rounds;
}

/** 中间轮模型输出（过程说明）：弱化排版，不与终答混排 */
function RoundText({ text }: { text: string }) {
  return (
    <Typography.Paragraph
      type="secondary"
      style={{ fontSize: 13, whiteSpace: "pre-wrap", margin: "0 0 4px" }}
    >
      {text}
    </Typography.Paragraph>
  );
}

// 预算使用：从 run 落库的 budget_used 读（SSE 无 budget_used 事件；
// runtime 在暂停/终态时落库，运行中为空不显示）
function extractBudgetUsed(run: RunOut | undefined): {
  iterations: number;
  max: number;
  tokens: number;
} | null {
  const used = run?.budget_used as Record<string, number> | undefined;
  const budget = run?.budget as Record<string, number> | undefined;
  if (!used) return null;
  return {
    iterations: (used.iterations as number) ?? 0,
    max: (budget?.max_iterations as number) ?? 0,
    tokens: ((used.input_tokens as number) ?? 0) + ((used.output_tokens as number) ?? 0),
  };
}

/** 等待超时点（ISO → 本地时分秒），无效/缺失返回空串。 */
function fmtClock(v: unknown): string {
  if (typeof v !== "string" || !v) return "";
  const t = new Date(v);
  return Number.isNaN(t.getTime()) ? "" : t.toLocaleTimeString("zh-CN");
}

/** 等待时长（毫秒 → 人话），缺省为“未知”。 */
function fmtWaited(v: unknown): string {
  const ms = Number(v ?? 0);
  if (!ms || ms <= 0) return "未知";
  return ms >= 60000 ? `${Math.round(ms / 60000)} 分钟` : `${Math.round(ms / 1000)} 秒`;
}

function MessageItem({ msg }: { msg: MessageOut }) {
  const isUser = msg.role === "user";
  const atts = isUser ? msgAttachments(msg) : [];
  const text = msgText(msg);
  const setCtxRun = useSSEStore((s) => s.setActiveRun);
  const openContextPanel = useUIStore((s) => s.openContextPanel);
  if (isUser) {
    // 用户消息：蓝色气泡右对齐（短内容，气泡式合适）
    return (
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          marginBottom: 12,
        }}
      >
        <div
          className="msg-bubble"
          style={{
            maxWidth: "80%",
            padding: "8px 14px",
            borderRadius: 8,
            background: "var(--ant-color-primary)",
            color: "#fff",
          }}
        >
          <AttachmentList atts={atts} />
          {text ? (
            <div style={{ whiteSpace: "pre-wrap", fontSize: 14 }}>{text}</div>
          ) : null}
        </div>
      </div>
    );
  }
  // assistant 回复：全宽文档式（无气泡）——内容本质是文档
  // （表格/代码块/长列表），全宽排版更舒展（ChatGPT/Claude 同款）
  return (
    <div className="msg-doc" style={{ marginBottom: 12 }}>
      <MarkdownRenderer content={text} />
      {/* 消息 ↔ 任务关联入口：本条回复出自哪个 run，点开右栏看执行过程 */}
      {msg.run_id && (
        <div style={{ marginTop: 4 }}>
          <Button
            type="text"
            size="small"
            className="msg-run-link"
            icon={<ProfileOutlined />}
            onClick={() => {
              setCtxRun(msg.run_id!);
              openContextPanel();
            }}
            style={{ fontSize: 11, padding: "0 4px", height: 20 }}
          >
            任务详情 · {msg.run_id.slice(0, 8)}
          </Button>
        </div>
      )}
    </div>
  );
}

/** 确认事件是否已被用户处理：其后出现过 run_status(running)（confirm 恢复）即已处理。 */
export function isConfirmationResolved(events: RunEventOut[], seq: number): boolean {
  return events.some(
    (x) =>
      x.seq > seq &&
      x.event_type === "run_status" &&
      (x.payload.status as string) === "running",
  );
}

/** 同一次工具调用与其结果合并为一条事件：分开展示「🛠 调用 / ✅ 结果」两条是噪音，
 * 同一次调用本就是同一个可展开单元。规则：按工具名先进先出配对，结果挂进
 * 对应 tool_call 的 payload（result/ok/elapsed_ms）；配不上对的调用保持无结果
 * （执行中/丢失），配不上对的结果保留原事件单独渲染。
 * 返回浅拷贝，不改 SSE store 里的原事件对象。 */
export function mergeToolEvents(events: RunEventOut[]): RunEventOut[] {
  const pending = new Map<string, RunEventOut[]>();
  const out: RunEventOut[] = [];
  for (const e of events) {
    if (e.event_type === "tool_call") {
      const name = String(e.payload.tool_name ?? e.payload.name ?? "unknown");
      const clone = { ...e, payload: { ...e.payload } };
      const queue = pending.get(name) ?? [];
      queue.push(clone);
      pending.set(name, queue);
      out.push(clone);
    } else if (e.event_type === "tool_result") {
      const name = String(e.payload.name ?? e.payload.tool_name ?? "");
      const target = pending.get(name)?.shift();
      if (target) {
        target.payload.result = e.payload.result ?? e.payload.content ?? "";
        target.payload.ok = e.payload.ok ?? true;
        if (e.payload.elapsed_ms != null) target.payload.elapsed_ms = e.payload.elapsed_ms;
      } else {
        out.push({ ...e, payload: { ...e.payload } });
      }
    } else {
      out.push(e);
    }
  }
  return out;
}

export function EventItem({
  event,
  resolved,
}: {
  event: RunEventOut;
  /** confirmation_request 已被用户确认（run 已恢复）：标签翻转为已确认 */
  resolved?: boolean;
}) {
  const { event_type, payload } = event;

  if (event_type === "message_delta") return null; // 已累积渲染

  if (event_type === "thought") {
    // 后端 payload：{tool_calls: [{name, args}]}——本轮思考选择的工具；
    // text/content 为兼容将来纯文本思考的兑底
    const toolCalls = (payload.tool_calls ?? []) as { name: string; args: unknown }[];
    const text = (payload.text ?? payload.content ?? "") as string;
    return (
      <Collapse
        size="small"
        style={{ marginBottom: 4, border: "none" }}
        items={[{
          key: "1",
          label: (
            <Space size={6}>
              <BulbOutlined style={{ color: "var(--ant-color-warning)" }} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                思考过程{toolCalls.length > 0 ? ` · 选择 ${toolCalls.length} 个工具` : ""}
              </Typography.Text>
            </Space>
          ),
          children:
            toolCalls.length > 0 ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {toolCalls.map((c, i) => (
                  <Typography.Text
                    key={i}
                    type="secondary"
                    style={{ fontSize: 13 }}
                    className="font-mono-tight"
                  >
                    决定调用 {c.name}
                  </Typography.Text>
                ))}
              </div>
            ) : (
              <Typography.Paragraph
                style={{ fontSize: 13, whiteSpace: "pre-wrap", margin: 0 }}
                type="secondary"
              >
                {text}
              </Typography.Paragraph>
            ),
        }]}
      />
    );
  }

  if (event_type === "plan_updated") {
    // 后端 payload：{plan_ref, items: [{seq, text, status}]}
    const tasks = (payload.items ?? payload.tasks ?? []) as {
      seq?: number;
      task?: string;
      text?: string;
      done?: boolean;
      status?: string;
    }[];
    return (
      <div
        style={{
          marginBottom: 8,
          padding: "8px 12px",
          borderRadius: 6,
          background: "rgba(22,119,255,0.06)",
          borderLeft: "3px solid var(--ant-color-primary)",
        }}
      >
        <Space size={6} style={{ marginBottom: 4 }}>
          <ScheduleOutlined />
          <Typography.Text strong style={{ fontSize: 13 }}>执行计划</Typography.Text>
        </Space>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {tasks.map((t, i) => {
            const done = t.done ?? t.status === "done";
            const text = t.text ?? t.task ?? "";
            return (
              <div key={i} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
                <span style={{ color: done ? "var(--ant-color-success)" : "var(--ant-color-text-quaternary)" }}>
                  {done ? "☑" : "○"}
                </span>
                <span style={{ textDecoration: done ? "line-through" : "none" }}>
                  {t.seq ? `${t.seq}. ` : ""}{text}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  if (event_type === "tool_call") {
    // 调用与结果合并展示（mergeToolEvents 已把配对的 tool_result 挂进 payload）：
    // 展开一个单元就能看到「参数 → 结果」全貌；无 result 字段 = 执行中或结果丢失
    const name = (payload.tool_name ?? payload.name ?? "unknown") as string;
    const args = payload.args ?? payload.arguments;
    const result = payload.result as string | undefined;
    const ok = (payload.ok as boolean) ?? true;
    const elapsed = payload.elapsed_ms as number | undefined;
    const resultStr =
      result === undefined ? "" : typeof result === "string" ? result : JSON.stringify(result, null, 2);
    return (
      <Collapse
        size="small"
        style={{ marginBottom: 4, border: "none" }}
        items={[{
          key: "1",
          label: (
            <Space size={6}>
              <ToolOutlined
                style={{
                  color: result === undefined
                    ? "var(--ant-color-text-secondary)"
                    : ok
                      ? "var(--ant-color-success)"
                      : "var(--ant-color-error)",
                }}
              />
              <Typography.Text style={{ fontSize: 12 }} className="font-mono-tight">
                {name}
              </Typography.Text>
              {result !== undefined && (
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  {ok ? "已完成" : "出错"}
                  {typeof elapsed === "number" ? ` · ${(elapsed / 1000).toFixed(1)}s` : ""}
                </Typography.Text>
              )}
            </Space>
          ),
          children: (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  调用参数
                </Typography.Text>
                <pre
                  className="font-mono-tight"
                  style={{ fontSize: 12, margin: 0, whiteSpace: "pre-wrap" }}
                >
                  {JSON.stringify(args, null, 2)}
                </pre>
              </div>
              {result !== undefined && (
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    执行结果
                  </Typography.Text>
                  <pre
                    className="font-mono-tight"
                    style={{
                      fontSize: 12,
                      margin: 0,
                      whiteSpace: "pre-wrap",
                      maxHeight: 200,
                      overflow: "auto",
                    }}
                  >
                    {resultStr.slice(0, 2000)}
                  </pre>
                </div>
              )}
            </div>
          ),
        }]}
      />
    );
  }

  if (event_type === "tool_result") {
    // 后端 payload：{tool, ok, elapsed_ms, result, error?}（P0-3 起）
    const name = (payload.tool ?? payload.name ?? payload.tool_name ?? "") as string;
    const result = payload.result ?? payload.content ?? "";
    const resultStr = typeof result === "string" ? result : JSON.stringify(result, null, 2);
    const failed = payload.ok === false;
    const err = failed ? describeError(payload.error) : null;
    return (
      <Collapse
        size="small"
        style={{ marginBottom: 4, border: "none" }}
        items={[{
          key: "1",
          label: (
            <Space size={6}>
              <ToolOutlined
                style={{
                  color: failed ? "var(--ant-color-error)" : "var(--ant-color-success)",
                }}
              />
              <Typography.Text
                type={failed ? "danger" : "secondary"}
                style={{ fontSize: 12 }}
              >
                {name} · {failed ? `失败 [${err?.code}]` : "结果"}
              </Typography.Text>
            </Space>
          ),
          children: (
            <>
              {err && (
                <Typography.Text type="danger" style={{ fontSize: 12 }}>
                  {err.title}
                  {err.retryable ? "（可重试）" : ""}
                  {err.detail ? `：${err.detail.replace(/\s+/g, " ").slice(0, 160)}` : ""}
                </Typography.Text>
              )}
              <pre
                className="font-mono-tight"
                style={{
                  fontSize: 12,
                  margin: 0,
                  whiteSpace: "pre-wrap",
                  maxHeight: 200,
                  overflow: "auto",
                }}
              >
                {resultStr.slice(0, 2000)}
              </pre>
            </>
          ),
        }]}
      />
    );
  }

  if (event_type === "context_compacted") {
    return (
      <Tag color="orange" style={{ marginBottom: 4, fontSize: 11 }}>
        上下文已压缩
      </Tag>
    );
  }

  if (event_type === "confirmation_request") {
    const reason = payload.reason as string;
    if (resolved) {
      return (
        <Tooltip title="你已处理此确认，Agent 已继续执行">
          <Tag color="success" style={{ marginBottom: 4, fontSize: 11 }}>
            {reason === "high_risk_tool" ? "高危操作已确认" : "计划已确认"}
          </Tag>
        </Tooltip>
      );
    }
    return (
      <Tooltip title="Agent 已暂停，等待你在下方确认">
        <Tag color={reason === "high_risk_tool" ? "error" : "warning"} style={{ marginBottom: 4, fontSize: 11 }}>
          {reason === "high_risk_tool" ? "高危操作待确认" : "计划待确认"}
        </Tag>
      </Tooltip>
    );
  }

  if (event_type === "card") {
    // 结果卡（P0-5）：事实进事件流，历史重放即重建卡面；
    // 卡面只渲染元信息（正文已作为 assistant 消息渲染，不重复出文本）
    const cardType = String(payload.card_type ?? "generic");
    const inner = (payload.payload ?? {}) as Record<string, unknown>;
    return <CardRenderer payload={{ reason: cardType, payload: inner }} runId={event.run_id} />;
  }

  if (event_type === "await_started") {
    // P0-4：平台持有的外部等待开始（等谁 / 最晚等到什么时候）
    const tool = String(payload.tool ?? "外部流程");
    const deadline = fmtClock(payload.deadline_at);
    return (
      <Tooltip
        title={`外部流程 ${tool} 已受理本次请求，平台代为等待回调${
          deadline ? `，最晚 ${deadline} 自动超时` : ""
        }`}
      >
        <Tag color="processing" style={{ marginBottom: 4, fontSize: 11 }}>
          等待外部回调：{tool}
          {deadline ? `（最晚 ${deadline}）` : ""}
        </Tag>
      </Tooltip>
    );
  }

  if (event_type === "await_resolved") {
    // 回调成功与人工撤销共用本事件，用 status 区分
    const tool = String(payload.tool ?? "外部流程");
    const status = String(payload.status ?? "granted");
    if (status === "cancelled") {
      return (
        <Tag color="default" style={{ marginBottom: 4, fontSize: 11 }}>
          外部等待已撤销：{tool}
        </Tag>
      );
    }
    return (
      <Tooltip title={`等待 ${fmtWaited(payload.waited_ms)}，结果已注入本轮上下文`}>
        <Tag color="success" style={{ marginBottom: 4, fontSize: 11 }}>
          外部回调已返回：{tool}
        </Tag>
      </Tooltip>
    );
  }

  if (event_type === "await_expired") {
    const tool = String(payload.tool ?? "外部流程");
    return (
      <Tooltip title={`等待 ${fmtWaited(payload.waited_ms)} 仍未收到回调，已按超时处理并继续执行`}>
        <Tag color="warning" style={{ marginBottom: 4, fontSize: 11 }}>
          外部等待超时：{tool}
        </Tag>
      </Tooltip>
    );
  }

  if (event_type === "blocked") {
    // 支线受阻（P1-6）：收敛原因枚举化后即可直接展示，不必再翻看板猜
    const name = String(payload.name ?? "支线");
    const reason = STEP_BLOCK_REASON_LABELS[String(payload.reason ?? "")] ?? "原因未标注";
    return (
      <Tooltip title={`${name} 受阻（${reason}）：可在任务卡上关闭支线 / 重新排队 / 转人工`}>
        <Tag color="volcano" style={{ marginBottom: 4, fontSize: 11 }}>
          支线受阻：{name}（{reason}）
        </Tag>
      </Tooltip>
    );
  }

  if (event_type === "unblocked") {
    const name = String(payload.name ?? "支线");
    const action = STEP_CONVERGE_LABELS[String(payload.action ?? "")] ?? "已收敛";
    return (
      <Tag color="success" style={{ marginBottom: 4, fontSize: 11 }}>
        支线已收敛：{name}（{action}）
      </Tag>
    );
  }

  if (event_type === "run_status") {
    const status = payload.status as string;
    if (status === "paused_awaiting_confirm") {
      return (
        <Tag color="warning" style={{ marginBottom: 4, fontSize: 11 }}>
          等待确认
        </Tag>
      );
    }
    if (status === "waiting_external") {
      return (
        <Tag color="processing" style={{ marginBottom: 4, fontSize: 11 }}>
          等待外部回调
        </Tag>
      );
    }
    const color =
      status === "done" ? "success" :
      status === "failed" || status === "aborted" || status === "timeout" ? "error" :
      "processing";
    return (
      <Tag color={color} style={{ marginBottom: 4, fontSize: 11 }}>
        运行 {status}
      </Tag>
    );
  }

  if (event_type === "budget_warning") {
    return (
      <Tooltip title={(payload.detail as string) ?? ""}>
        <Tag color="warning" style={{ marginBottom: 4, fontSize: 11 }}>
          预算告警：{String(payload.gate ?? "")}
        </Tag>
      </Tooltip>
    );
  }

  if (event_type === "capability_overflow") {
    // 必得能力集装不下（P0-2）：平台不静默切片，而是把"哪些工具被丢"摆到台面上
    const dropped = (payload.dropped ?? []) as string[];
    const required = Number(payload.required_count ?? 0);
    const budget = Number(payload.tool_budget ?? 0);
    return (
      <Tooltip
        title={
          dropped.length > 0
            ? `被挤出的工具：${dropped.join("、")}`
            : "必得工具已超出预算，共享区名额为 0"
        }
      >
        <Tag color="volcano" style={{ marginBottom: 4, fontSize: 11 }}>
          工具容量不足：必得 {required} 个 / 预算 {budget}
          {dropped.length > 0 ? `，${dropped.length} 个候选未装配` : ""}
        </Tag>
      </Tooltip>
    );
  }

  if (event_type === "error") {
    const err = describeError(payload);
    return (
      <Tag color="error" style={{ marginBottom: 4, fontSize: 11 }}>
        错误：{err.title}
        {err.detail ? `：${err.detail.replace(/\s+/g, " ").slice(0, 120)}` : ""}
        {err.retryable ? "（可重试）" : ""}
      </Tag>
    );
  }

  return null;
}

// 终态 run 集合：这些 run 的执行过程属于「历史」，可随时从事件表回放
const TERMINAL_RUN_STATUSES = new Set(["done", "failed", "cancelled", "aborted", "timeout"]);

/** 执行计划卡：只渲染最新一份（plan_updated 在单个 run 内会多次发射——
 *  planner 首发 + 每次子任务状态变化 emit_task_steps 再发，逐事件渲染
 *  会导致一屏多张重复卡，bug 根因即在此）。取最后一条，单卡随事件更新。 */
function PlanCard({ payload }: { payload: Record<string, unknown> }) {
  const tasks = (payload.items ?? payload.tasks ?? []) as {
    seq?: number;
    task?: string;
    text?: string;
    done?: boolean;
    status?: string;
    kind?: string;
  }[];
  const progress = payload.progress as
    | { done?: number; total?: number; label?: string }
    | undefined;
  if (tasks.length === 0) return null;
  return (
    <div
      style={{
        marginBottom: 8,
        padding: "8px 12px",
        borderRadius: 8,
        background: "rgba(22,119,255,0.05)",
        borderLeft: "3px solid var(--ant-color-primary)",
      }}
    >
      <Space size={6} style={{ marginBottom: 4 }}>
        <ScheduleOutlined style={{ color: "var(--ant-color-primary)" }} />
        <Typography.Text strong style={{ fontSize: 13 }}>
          执行计划
        </Typography.Text>
        {progress && progress.total ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {progress.done ?? 0}/{progress.total}
            {progress.label ? ` · ${progress.label}` : ""}
          </Typography.Text>
        ) : null}
      </Space>
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {tasks.map((t, i) => {
          const done = t.done ?? t.status === "done";
          const text = t.text ?? t.task ?? "";
          return (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
              <span style={{ color: done ? "var(--ant-color-success)" : "var(--ant-color-text-quaternary)" }}>
                {done ? "☑" : "○"}
              </span>
              <span
                style={{
                  textDecoration: done ? "line-through" : "none",
                  color: done ? "var(--ant-color-text-tertiary)" : undefined,
                }}
              >
                {t.seq ? `${t.seq}. ` : ""}{t.kind === "branch" ? "[支线] " : ""}{text}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 执行过程收纳面板：收起态一行摘要，展开后逐条渲染 EventItem。 */
function ExecutionTrace({ events, running }: { events: RunEventOut[]; running: boolean }) {
  const [open, setOpen] = useState(false);
  if (events.length === 0) return null;
  const toolCalls = events.filter((e) => e.event_type === "tool_call").length;
  const thoughts = events.filter((e) => e.event_type === "thought").length;
  return (
    <div className="exec-trace" style={{ marginBottom: 8 }}>
      <div className="exec-trace-header" onClick={() => setOpen((v) => !v)}>
        <Space size={6}>
          {running ? (
            <LoadingOutlined spin style={{ color: "var(--ant-color-primary)" }} />
          ) : (
            <ToolOutlined style={{ color: "var(--ant-color-text-tertiary)" }} />
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            执行过程
          </Typography.Text>
          {toolCalls > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              · {toolCalls} 次工具调用
            </Typography.Text>
          )}
          {thoughts > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              · {thoughts} 轮思考
            </Typography.Text>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            {open ? "收起" : "展开"}
          </Typography.Text>
          {open ? <DownOutlined style={{ fontSize: 10 }} /> : <RightOutlined style={{ fontSize: 10 }} />}
        </Space>
      </div>
      {open && (
        <div className="exec-trace-body" style={{ padding: "4px 0 4px 12px" }}>
          {events.map((e) => (
            <EventItem key={e.seq} event={e} />
          ))}
        </div>
      )}
    </div>
  );
}

/** 历史 run 的执行过程块：终态 run 的事件也已落库（GET /runs/{id}/events），
 * 刷新/切页再进会话仍能看到当时的思考与工具调用（不再随内存丢失）。
 * 按轮次分段展示：中间轮的过程说明 + 每轮独立的执行过程收纳块；
 * 终答文本由消息流里的 assistant 消息渲染（hasReply 时不重复展示）。 */
/** 落库 assistant 消息的纯文本（双渲染去重的比对基准） */
function messageText(m: MessageOut): string {
  const c = m.content as unknown;
  if (typeof c === "string") return c;
  const t = (c as Record<string, unknown> | null)?.text;
  return typeof t === "string" ? t : "";
}

/**
 * 轮次文本是否已作为落库 assistant 消息渲染过：开头 60 字符被某条
 * 已渲染消息包含即视为重复（旧数据无 message_reset 时轮文本会拼接下轮
 * 开头，取前缀比对可容）。命中则过程区不再重复灰字，防双渲染。
 */
function repliedHit(text: string, repliedTexts: string[]): boolean {
  const head = text.trim().slice(0, 60);
  if (!head) return false;
  return repliedTexts.some((m) => m.includes(head));
}

function RunTraceBlock({ runId, repliedTexts }: { runId: string; repliedTexts: string[] }) {
  const { data: events = [] } = useQuery({
    queryKey: ["run-events", runId],
    queryFn: () => runsApi.events(runId),
    staleTime: 5 * 60_000,
  });
  const rounds = splitRounds(events);
  // 结果卡（P0-5）：终态 run 的历史重放靠它，只取最后一份（重放/重试不重复渲染）
  const resultCard = [...events].reverse().find((e) => e.event_type === "card");
  // hasReply：该 run 的终答已作为消息渲染（防终答双渲染）；中间轮文本
  // 已落库（如 verify 重试产生多条消息）同样要去重
  const hasReply = repliedTexts.length > 0;
  const lastIdx = rounds.length - 1;
  const anything =
    !!resultCard ||
    rounds.some(
      (r, i) =>
        r.traceEvents.length > 0 ||
        (i === lastIdx ? !hasReply && !!r.text : !!r.text && !repliedHit(r.text, repliedTexts)),
    );
  if (!anything) return null;
  return (
    <>
      {rounds.map((r, i) => (
        <Fragment key={i}>
          {r.traceEvents.length > 0 && r.text && !repliedHit(r.text, repliedTexts) && (
            <RoundText text={r.text} />
          )}
          {r.traceEvents.length > 0 && (
            <ExecutionTrace events={mergeToolEvents(r.traceEvents)} running={false} />
          )}
          {/* 无回复消息的终答残留（如 failed）：也展示出来 */}
          {i === lastIdx && r.traceEvents.length === 0 && !hasReply && r.text && (
            <RoundText text={r.text} />
          )}
        </Fragment>
      ))}
      {resultCard && <EventItem event={resultCard} />}
    </>
  );
}

function LiveRun({ runId, repliedTexts }: { runId: string; repliedTexts: string[] }) {
  // replied：历史消息已含本 run 的 assistant 回复（终态后 messages 刷新到达）。
  // 此时隐藏流式气泡交给 MessageItem，避免同一回复双渲染；中间轮文本
  // 已落库（verify 重试等多终答场景）同样去重。
  const replied = repliedTexts.length > 0;
  const { eventsByRun, statusByRun } = useSSEStore();
  const events = eventsByRun[runId] ?? [];
  const sseStatus = statusByRun[runId];
  const running = sseStatus === "live" || sseStatus === "connecting" || sseStatus == null;

  // 同时拉取 run 详情：预算指示读 budget_used 落库值
  const { data: run } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => runsApi.get(runId),
    refetchInterval: 3000,
  });
  const budget = extractBudgetUsed(run);

  // 分流渲染（按轮次分段）：每轮「过程说明文本 + 执行过程收纳」；
  // 执行计划单卡 + 错误/待确认醒目保留；终答轮文本流式渲染
  const rounds = splitRounds(events);
  const lastRound = rounds[rounds.length - 1];
  // 终答轮：最后一个无过程事件的轮；有过程事件的末轮文本是过程说明（工具还在跑）
  const replyText = lastRound && lastRound.traceEvents.length === 0 ? lastRound.text : "";
  const planEvent = [...events].reverse().find((e) => e.event_type === "plan_updated");
  // P1-5：平台 watcher 推送的进度快照（非模型轮次）。计划卡随模型轮次刷新，但进度
  // 数字必须取**更新的那一份**：等待外部回调/前台收敛支线期间只有 progress 会到达
  // （不取它看板就停在原地），而模型产出新计划时又不能被上一条旧快照覆盖回落后值。
  const progressEvent = [...events].reverse().find((e) => e.event_type === "progress");
  const planPayload =
    planEvent && progressEvent && progressEvent.seq > planEvent.seq
      ? { ...planEvent.payload, progress: progressEvent.payload }
      : (planEvent?.payload ?? null);
  const notableEvents = events.filter(
    (e) =>
      e.event_type === "error" ||
      e.event_type === "confirmation_request" ||
      e.event_type === "card" ||
      e.event_type === "capability_overflow" ||
      // P1-6：支线受阻与前台收敛是任务状态变化，必须留在会话流里（可追溯谁收敛的）
      e.event_type === "blocked" ||
      e.event_type === "unblocked" ||
      // P0-4：外部等待的开始/结束是用户需要看见的状态变化（不是可折叠的过程噪声）
      e.event_type === "await_started" ||
      e.event_type === "await_resolved" ||
      e.event_type === "await_expired",
  );

  // 提交后空窗期反馈（Kimi 式）：不能只看 events.length —— 后端首发事件往往是
  // run_status(running)（不在收纳类型里），到达后“无事件”条件即失效，而首个
  // thought 要等 LLM 首轮返回（可能几十秒）才出现，主区会完全空窗。
  // 改为看「无实质反馈」：任何轮次内容/计划一个都没有时保持“正在思考”。
  const waiting = running && !replied && rounds.length === 0 && !planEvent;

  return (
    <div style={{ marginBottom: 12 }}>
      {/* 提交反馈：正在思考动效（首个事件到达前） */}
      {waiting && (
        <div className="thinking-indicator">
          <span className="thinking-text">正在思考</span>
          <span className="thinking-dots">
            <i />
            <i />
            <i />
          </span>
        </div>
      )}

      {/* 预算指示：小字弱化，不抢占视觉 */}
      {budget && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            marginBottom: 4,
            padding: "2px 8px",
            fontSize: 11,
            color: "var(--ant-color-text-tertiary)",
          }}
        >
          <span>迭代 {budget.iterations}/{budget.max}</span>
          <span>token {budget.tokens.toLocaleString()}</span>
          {run?.budget && (
            <span>
              工具预算 {String((run.budget as Record<string, unknown>).tool_budget ?? "?")}
            </span>
          )}
        </div>
      )}

      {/* 每轮分段：过程说明（弱化，已落库的跳过）+ 本轮执行过程收纳（调用/结果已合并） */}
      {rounds.map((r, i) => (
        <Fragment key={i}>
          {r.traceEvents.length > 0 && r.text && !repliedHit(r.text, repliedTexts) && (
            <RoundText text={r.text} />
          )}
          {r.traceEvents.length > 0 && (
            <ExecutionTrace
              events={mergeToolEvents(r.traceEvents)}
              running={running && i === rounds.length - 1}
            />
          )}
        </Fragment>
      ))}
      
      {/* 执行计划：只渲染最新一份，随 plan_updated 实时更新；进度数字以 P1-5 的
          `progress` 事件为准（平台侧推送，含等待外部回调期间的快照） */}
      {planPayload && <PlanCard payload={planPayload} />}

      {/* 错误与待确认：醒目展示，不收纳；已被用户处理的确认翻转为已确认 */}
      {notableEvents.map((e) => (
        <EventItem
          key={e.seq}
          event={e}
          resolved={
            e.event_type === "confirmation_request" && isConfirmationResolved(events, e.seq)
          }
        />
      ))}

      {/* 流式 Agent 回复（仅终答轮文本，中间轮已随执行过程分段展示）：
          历史消息未接管时才渲染（终态交接，防双渲染）。assistant 全宽文档式 */}
      {replyText && !replied && (
        <div className="msg-doc" style={{ marginBottom: 12 }}>
          <MarkdownRenderer content={replyText} />
          <span className="cursor-blink">▎</span>
        </div>
      )}
    </div>
  );
}

export default function MessageStream({
  convId,
  activeRun,
  optimistic,
}: {
  convId: string;
  activeRun: ActiveRun | null;
  /** 乐观用户消息（发送即上屏）：真实消息落库后自动接管，避免双渲染 */
  optimistic?: { text: string; key: string } | null;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  // 与后端 list_messages 默认 limit 一致：首次拉满一页才可能还有更早历史
  const PAGE_LIMIT = 200;
  const { data: messages = [] } = useQuery({
    queryKey: ["messages", convId],
    queryFn: () => conversationsApi.messages(convId),
  });
  // 本会话全部 run（含终态）：历史执行过程穿插渲染的数据源（需求 4）。
  // 历史事件不可变，staleTime 放宽降低重复拉取
  const { data: convRuns = [] } = useQuery({
    queryKey: ["runs", "conv", convId],
    queryFn: () => runsApi.list({ conversation_id: convId, limit: 100 }),
    staleTime: 60_000,
  });
  // 向前翻页的更早消息（本地叠加，不进 react-query 缓存：切会话即弃）
  const [older, setOlder] = useState<MessageOut[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // 加载更早后的滚动位置恢复（避免 prepend 导致视口跳底）
  const scrollRestore = useRef<{ top: number; height: number } | null>(null);

  useEffect(() => {
    setOlder([]);
    setHasMore(false);
  }, [convId]);

  useEffect(() => {
    setHasMore(messages.length >= PAGE_LIMIT);
  }, [messages]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, activeRun, optimistic]);

  // prepend 后保持视口停在原内容处（滚动容器是父级 overflow:auto 的 div）
  useEffect(() => {
    if (!scrollRestore.current) return;
    const container = rootRef.current?.parentElement;
    if (container) {
      container.scrollTop =
        container.scrollHeight - scrollRestore.current.height + scrollRestore.current.top;
    }
    scrollRestore.current = null;
  }, [older]);

  const loadOlder = async () => {
    const first = older[0] ?? messages[0];
    if (!first || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const container = rootRef.current?.parentElement;
      if (container) {
        scrollRestore.current = { top: container.scrollTop, height: container.scrollHeight };
      }
      const prev = await conversationsApi.messages(convId, first.id);
      setOlder((cur) => [...prev, ...cur]);
      setHasMore(prev.length >= PAGE_LIMIT);
    } finally {
      setLoadingOlder(false);
    }
  };

  // older 与 messages 可能在窗口滑动后重叠（新消息到达使最新一页前移），按 id 去重
  const all = (() => {
    const byId = new Map<string, MessageOut>();
    for (const m of [...older, ...messages]) byId.set(m.id, m);
    return Array.from(byId.values());
  })();

  return (
    <div style={{ padding: "0 16px" }} ref={rootRef}>
      {hasMore && all.length > 0 && (
        <div style={{ textAlign: "center", padding: "8px 0" }}>
          <Button size="small" loading={loadingOlder} onClick={loadOlder}>
            加载更早的消息
          </Button>
        </div>
      )}
      {all.map((msg, idx) => {
        // 终态历史 run 的思考/工具调用穿插在消息间（需求 3/4）：挂在触发它的
        // 用户消息之后、下一条用户消息之前——多次触发就分多段展开，各自收起。
        // 注意用 >=：run 与触发它的用户消息在同一事务创建，时间戳完全相同
        const nextUser = all.slice(idx + 1).find((m) => m.role === "user");
        const historyRuns =
          msg.role === "user"
            ? convRuns.filter(
                (r) =>
                  r.id !== activeRun?.runId &&
                  TERMINAL_RUN_STATUSES.has(r.status) &&
                  r.created_at >= msg.created_at &&
                  (!nextUser || r.created_at < nextUser.created_at),
              )
            : [];
        return (
          <Fragment key={msg.id}>
            <MessageItem msg={msg} />
            {historyRuns.map((r) => (
              <RunTraceBlock
                key={r.id}
                runId={r.id}
                repliedTexts={all
                  .filter((m) => m.role === "assistant" && m.run_id === r.id)
                  .map(messageText)}
              />
            ))}
          </Fragment>
        );
      })}

      {/* 乐观用户消息：提交即上屏（Kimi 式即时反馈）；真实消息落库到达后自动隐藏。
          半透明微降表示“发送中”，接管后完全态 */}
      {optimistic &&
        !messages.some(
          (m) =>
            m.role === "user" &&
            ((m.content as Record<string, unknown>).text ?? "") === optimistic.text,
        ) && (
          <div
            key={optimistic.key}
            style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}
          >
            <div
              className="msg-bubble"
              style={{
                maxWidth: "80%",
                padding: "8px 14px",
                borderRadius: 8,
                background: "var(--ant-color-primary)",
                color: "#fff",
                opacity: 0.75,
              }}
            >
              <div style={{ whiteSpace: "pre-wrap", fontSize: 14 }}>{optimistic.text}</div>
            </div>
          </div>
        )}

      {/* 实时运行事件：终态后保留（执行过程历史消息不存，刷新前可查） */}
      {activeRun && (
        <LiveRun
          runId={activeRun.runId}
          repliedTexts={all
            .filter((m) => m.role === "assistant" && m.run_id === activeRun.runId)
            .map(messageText)}
        />
      )}

      <div ref={bottomRef} />
    </div>
  );
}
