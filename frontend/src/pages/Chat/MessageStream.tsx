/**
 * 消息流（前端设计 §3.1 主区）。
 * 历史消息 + 实时 SSE 事件 + 流式 Agent 回复。
 */
import { useRef, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Typography, Tag, Collapse, Tooltip, Space, Image, Button } from "antd";
import {
  BulbOutlined,
  ToolOutlined,
  ScheduleOutlined,
  RobotOutlined,
  PaperClipOutlined,
  ProfileOutlined,
} from "@ant-design/icons";
import { conversationsApi } from "@/api/conversations";
import { filesApi } from "@/api/files";
import { useSSEStore } from "@/store/sse";
import { useUIStore } from "@/store/ui";
import { runsApi } from "@/api/runs";
import MarkdownRenderer from "@/components/MarkdownRenderer";
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

function fmtSize(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)}MB`;
  if (size >= 1024) return `${Math.round(size / 1024)}KB`;
  return `${size}B`;
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
      <div style={{ width: 96, height: 96, borderRadius: 6, background: "rgba(255,255,255,0.2)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11 }}>
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

// 从 SSE 事件流累积 message_delta → 完整文本（后端 payload 字段为 text）
function accumulateDeltas(events: RunEventOut[]): string {
  let text = "";
  for (const e of events) {
    if (e.event_type === "message_delta") {
      const delta = (e.payload.text ?? e.payload.delta) as string;
      if (delta) text += delta;
    }
  }
  return text;
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

function MessageItem({ msg }: { msg: MessageOut }) {
  const isUser = msg.role === "user";
  const atts = isUser ? msgAttachments(msg) : [];
  const text = msgText(msg);
  const setCtxRun = useSSEStore((s) => s.setActiveRun);
  const openContextPanel = useUIStore((s) => s.openContextPanel);
  return (
    <div
      style={{
        display: "flex",
        justifyContent: isUser ? "flex-end" : "flex-start",
        marginBottom: 12,
      }}
    >
      <div
        className="msg-bubble"
        style={{
          maxWidth: "80%",
          padding: "8px 14px",
          borderRadius: 8,
          background: isUser
            ? "var(--ant-color-primary)"
            : "var(--ant-color-bg-container)",
          color: isUser
            ? "#fff"
            : "var(--ant-color-text)",
        }}
      >
        {isUser && <AttachmentList atts={atts} />}
        {isUser ? (
          text ? (
            <div style={{ whiteSpace: "pre-wrap", fontSize: 14 }}>{text}</div>
          ) : null
        ) : (
          <MarkdownRenderer content={text} />
        )}
        {/* 消息 ↔ 任务关联入口：本条回复出自哪个 run，点开右栏看执行过程 */}
        {!isUser && msg.run_id && (
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
    </div>
  );
}

export function EventItem({ event }: { event: RunEventOut }) {
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
                <span style={{ color: done ? "var(--ant-color-success)" : "rgba(128,128,128,0.5)" }}>
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
    const name = (payload.tool_name ?? payload.name ?? "unknown") as string;
    const args = payload.args ?? payload.arguments;
    return (
      <Collapse
        size="small"
        style={{ marginBottom: 4, border: "none" }}
        items={[{
          key: "1",
          label: (
            <Space size={6}>
              <ToolOutlined style={{ color: "var(--ant-color-text-secondary)" }} />
              <Typography.Text style={{ fontSize: 12 }} className="font-mono-tight">
                {name}
              </Typography.Text>
            </Space>
          ),
          children: (
            <pre
              className="font-mono-tight"
              style={{ fontSize: 12, margin: 0, whiteSpace: "pre-wrap" }}
            >
              {JSON.stringify(args, null, 2)}
            </pre>
          ),
        }]}
      />
    );
  }

  if (event_type === "tool_result") {
    // 后端 payload：{name, ok, elapsed_ms, content}
    const name = (payload.name ?? payload.tool_name ?? "") as string;
    const result = payload.result ?? payload.content ?? "";
    const resultStr = typeof result === "string" ? result : JSON.stringify(result, null, 2);
    return (
      <Collapse
        size="small"
        style={{ marginBottom: 4, border: "none" }}
        items={[{
          key: "1",
          label: (
            <Space size={6}>
              <ToolOutlined style={{ color: "var(--ant-color-success)" }} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {name} · 结果
              </Typography.Text>
            </Space>
          ),
          children: (
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
          ),
        }]}
      />
    );
  }

  if (event_type === "context_assembly") {
    return (
      <Tooltip title="上下文装配完成">
        <Tag style={{ marginBottom: 4, fontSize: 11 }}>
          <RobotOutlined /> 上下文已装配
        </Tag>
      </Tooltip>
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
    return (
      <Tooltip title="Agent 已暂停，等待你在下方确认">
        <Tag color={reason === "high_risk_tool" ? "error" : "warning"} style={{ marginBottom: 4, fontSize: 11 }}>
          {reason === "high_risk_tool" ? "高危操作待确认" : "计划待确认"}
        </Tag>
      </Tooltip>
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

  if (event_type === "error") {
    // 后端 payload：{code, detail}
    const msg = (payload.message ?? payload.detail ?? payload.error ?? "未知错误") as string;
    return (
      <Tag color="error" style={{ marginBottom: 4, fontSize: 11 }}>
        错误：{msg}
      </Tag>
    );
  }

  return null;
}

function LiveRun({ runId }: { runId: string }) {
  const { eventsByRun } = useSSEStore();
  const events = eventsByRun[runId] ?? [];

  const accumulated = accumulateDeltas(events);

  // 同时拉取 run 详情：预算指示读 budget_used 落库值
  const { data: run } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => runsApi.get(runId),
    refetchInterval: 3000,
  });
  const budget = extractBudgetUsed(run);

  return (
    <div style={{ marginBottom: 12 }}>
      {/* 预算指示 */}
      {budget && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            marginBottom: 8,
            padding: "4px 8px",
            fontSize: 11,
            color: "rgba(128,128,128,0.7)",
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

      {/* SSE 事件渲染 */}
      {events
        .filter((e) => e.event_type !== "message_delta")
        .map((e) => (
          <EventItem key={e.seq} event={e} />
        ))}

      {/* 流式 Agent 回复 */}
      {accumulated && (
        <div
          style={{
            display: "flex",
            justifyContent: "flex-start",
            marginBottom: 12,
          }}
        >
          <div
            style={{
              maxWidth: "80%",
              padding: "8px 14px",
              borderRadius: 8,
              background: "var(--ant-color-bg-container)",
            }}
          >
            <MarkdownRenderer content={accumulated} />
            <span className="cursor-blink">▎</span>
          </div>
        </div>
      )}
    </div>
  );
}

export default function MessageStream({
  convId,
  activeRun,
}: {
  convId: string;
  activeRun: ActiveRun | null;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  // 与后端 list_messages 默认 limit 一致：首次拉满一页才可能还有更早历史
  const PAGE_LIMIT = 200;
  const { data: messages = [] } = useQuery({
    queryKey: ["messages", convId],
    queryFn: () => conversationsApi.messages(convId),
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
  }, [messages, activeRun]);

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
      {all.map((msg) => (
        <MessageItem key={msg.id} msg={msg} />
      ))}

      {/* 实时运行事件 */}
      {activeRun && (
        <LiveRun runId={activeRun.runId} />
      )}

      <div ref={bottomRef} />
    </div>
  );
}
