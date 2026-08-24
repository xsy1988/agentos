/**
 * 消息流（前端设计 §3.1 主区）。
 * 历史消息 + 实时 SSE 事件 + 流式 Agent 回复。
 */
import { useRef, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Typography, Tag, Collapse, Tooltip, Space } from "antd";
import {
  BulbOutlined,
  ToolOutlined,
  ScheduleOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import { conversationsApi } from "@/api/conversations";
import { useSSEStore } from "@/store/sse";
import { runsApi } from "@/api/runs";
import MarkdownRenderer from "@/components/MarkdownRenderer";
import type { RunEventOut, MessageOut } from "@/api/types";
import type { ActiveRun } from "./index";

// 从消息 content 提取文本
function msgText(msg: MessageOut): string {
  const c = msg.content as Record<string, unknown>;
  return (c.text as string) ?? (c.content as string) ?? JSON.stringify(c);
}

// 从 SSE 事件流累积 message_delta → 完整文本
function accumulateDeltas(events: RunEventOut[]): string {
  let text = "";
  for (const e of events) {
    if (e.event_type === "message_delta") {
      const delta = e.payload.delta as string;
      if (delta) text += delta;
    }
  }
  return text;
}

// 提取预算使用
function extractBudgetUsed(events: RunEventOut[]): { iterations: number; max: number; tokens: number } | null {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].event_type === "budget_used") {
      return {
        iterations: events[i].payload.iterations as number,
        max: events[i].payload.max_iterations as number,
        tokens: events[i].payload.tokens as number,
      };
    }
  }
  return null;
}

function MessageItem({ msg }: { msg: MessageOut }) {
  const isUser = msg.role === "user";
  return (
    <div
      style={{
        display: "flex",
        justifyContent: isUser ? "flex-end" : "flex-start",
        marginBottom: 12,
      }}
    >
      <div
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
        {isUser ? (
          <div style={{ whiteSpace: "pre-wrap", fontSize: 14 }}>{msgText(msg)}</div>
        ) : (
          <MarkdownRenderer content={msgText(msg)} />
        )}
      </div>
    </div>
  );
}

function EventItem({ event }: { event: RunEventOut }) {
  const { event_type, payload } = event;

  if (event_type === "message_delta") return null; // 已累积渲染

  if (event_type === "thought") {
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
                思考过程
              </Typography.Text>
            </Space>
          ),
          children: (
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
    const name = (payload.tool_name ?? "") as string;
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

  if (event_type === "error") {
    const msg = (payload.message ?? payload.error ?? "未知错误") as string;
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
  const budget = extractBudgetUsed(events);

  // 同时拉取 run 详情获取预算信息
  const { data: run } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => runsApi.get(runId),
    refetchInterval: 3000,
  });

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
        .filter((e) => e.event_type !== "message_delta" && e.event_type !== "budget_used")
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
  const { data: messages = [] } = useQuery({
    queryKey: ["messages", convId],
    queryFn: () => conversationsApi.messages(convId),
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, activeRun]);

  return (
    <div style={{ padding: "0 16px" }}>
      {messages.map((msg) => (
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
