/**
 * 右侧任务详情面板（前端设计 §2.1）。
 * 消息流「任务详情」入口 / 发送消息自动关注后展示：
 * 任务输入、状态、耗时、预算消耗、执行计划、事件时间线（工具调用/思考/确认）。
 * 历史任务走 GET /runs/{id}/events 拉取，活跃任务叠加 SSE 实时事件（按 seq 去重合并）。
 */
import { useEffect, useMemo, useRef } from "react";
import type { CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { Empty, Typography, Button, Tag, Space, Spin, Tooltip } from "antd";
import { CloseOutlined, DownOutlined, UpOutlined } from "@ant-design/icons";
import { useSSEStore } from "@/store/sse";
import { useUIStore } from "@/store/ui";
import { runsApi } from "@/api/runs";
import { describeError } from "@/api/errors";
import PluginHost from "@/components/PluginHost";
import AwaitPanel, { hasUnresolvedAwait } from "@/components/AwaitPanel";
import CopyRefButton from "@/components/CopyRefButton";
import { runReference } from "@/utils/clipboard";
import { EventItem, isConfirmationResolved, mergeToolEvents } from "@/pages/Chat/MessageStream";
import type { RunOut, RunEventOut } from "@/api/types";

const TERMINAL_STATUSES = ["done", "failed", "cancelled", "aborted", "timeout"];

function statusTag(status: string) {
  const color =
    status === "done"
      ? "success"
      : status === "failed" || status === "aborted" || status === "timeout"
        ? "error"
        : status === "paused_awaiting_confirm"
          ? "warning"
          : "processing";
  const label: Record<string, string> = {
    done: "已完成",
    failed: "失败",
    aborted: "已中止",
    timeout: "超时",
    paused_awaiting_confirm: "等待确认",
    waiting_external: "等待外部回调",
    running: "运行中",
    pending: "排队中",
  };
  return <Tag color={color}>{label[status] ?? status}</Tag>;
}

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`;
}

function RunDetail({ runId }: { runId: string }) {
  const { eventsByRun } = useSSEStore();
  const liveEvents = eventsByRun[runId] ?? [];

  const { data: run } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => runsApi.get(runId),
    // 非终态轮询状态；终态停止（回调形式避免自引用）
    refetchInterval: (query) => {
      const data = query.state.data as RunOut | undefined;
      return !data || !TERMINAL_STATUSES.includes(data.status) ? 2000 : false;
    },
  });

  const { data: apiEvents = [], isLoading } = useQuery({
    queryKey: ["run-events", runId],
    queryFn: () => runsApi.events(runId),
    refetchInterval: (query) => {
      const data = query.state.data as RunOut | undefined;
      return !data || !TERMINAL_STATUSES.includes(data.status) ? 1500 : false;
    },
  });

  // 历史事件 + 实时事件按 seq 合并去重（SSE 断线续传也会重放，Map 天然去重）
  const events = useMemo(() => {
    const bySeq = new Map<number, RunEventOut>();
    for (const e of [...apiEvents, ...liveEvents]) bySeq.set(e.seq, e);
    return Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
  }, [apiEvents, liveEvents]);

  // 时间线展示用：合并同一次工具调用与结果（message_delta 已累积不展示）
  const mergedEvents = useMemo(
    () => mergeToolEvents(events.filter((e) => e.event_type !== "message_delta")),
    [events],
  );

  const meta: CSSProperties = { fontSize: 11, color: "var(--ant-color-text-secondary)" };
  // P0-4：run 停在外部等待，或事件里还有未落定的等待
  const awaiting = run?.status === "waiting_external" || hasUnresolvedAwait(events);

  const inputText = (run?.input?.text as string) ?? "";
  const budgetUsed = (run?.budget_used ?? {}) as Record<string, unknown>;
  const duration = run?.started_at
    ? new Date(run.finished_at ?? Date.now()).getTime() - new Date(run.started_at).getTime()
    : null;

  // 最新计划（plan_updated 取最后一次）
  const lastPlan = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].event_type === "plan_updated") return events[i].payload;
    }
    return null;
  }, [events]);

  if (!run) return <Spin style={{ display: "block", margin: "24px auto" }} />;

  return (
    <div>
      {/* 状态 + 元信息 */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        {statusTag(run.status)}
        <Typography.Text style={meta}>触发：{run.trigger}</Typography.Text>
        {duration !== null && <Typography.Text style={meta}>耗时 {fmtDuration(duration)}</Typography.Text>}
        <CopyRefButton text={runReference(run)} />
      </div>

      {/* P0-4：外部等待——等谁、最晚多久、可撤销（撤销后 run 以结构化失败收尾） */}
      {awaiting && <AwaitPanel runId={runId} />}

      {/* 任务输入 */}
      {inputText && (
        <div
          style={{
            marginTop: 8,
            padding: "6px 10px",
            borderRadius: 6,
            background: "var(--ant-color-fill-quaternary)",
            fontSize: 12,
            color: "var(--ant-color-text-secondary)",
            display: "-webkit-box",
            WebkitLineClamp: 3,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {inputText}
        </div>
      )}

      {/* 预算消耗 */}
      {(budgetUsed.iterations !== undefined || budgetUsed.input_tokens !== undefined) && (
        <div style={{ marginTop: 8, display: "flex", gap: 12, flexWrap: "wrap" }}>
          {budgetUsed.iterations !== undefined && (
            <Typography.Text style={meta}>
              迭代 {String(budgetUsed.iterations)}
              {budgetUsed.max_iterations !== undefined ? `/${String(budgetUsed.max_iterations)}` : ""}
            </Typography.Text>
          )}
          {budgetUsed.input_tokens !== undefined && (
            <Typography.Text style={meta}>
              输入 {Number(budgetUsed.input_tokens).toLocaleString()} tok
            </Typography.Text>
          )}
          {budgetUsed.output_tokens !== undefined && (
            <Typography.Text style={meta}>
              输出 {Number(budgetUsed.output_tokens).toLocaleString()} tok
            </Typography.Text>
          )}
          {budgetUsed.tool_calls !== undefined && (
            <Typography.Text style={meta}>工具 {String(budgetUsed.tool_calls)} 次</Typography.Text>
          )}
        </div>
      )}

      {/* 错误：P0-3 起 run.error 是结构化载荷，统一用 errors.ts 的文案表 */}
      {run.error && (
        <div
          style={{
            marginTop: 8,
            padding: "6px 10px",
            borderRadius: 6,
            background: "rgba(255,77,79,0.1)",
            fontSize: 12,
            color: "var(--ant-color-error)",
          }}
        >
          {(() => {
            const err = describeError(run.error);
            return `${err.title}${err.retryable ? "（可重试）" : ""}${err.detail ? `：${err.detail}` : ""}`;
          })()}
        </div>
      )}

      {/* 执行计划 */}
      {lastPlan && (
        <div style={{ marginTop: 12 }}>
          <Typography.Text strong style={{ fontSize: 12 }}>
            执行计划
          </Typography.Text>
          <div style={{ marginTop: 4, display: "flex", flexDirection: "column", gap: 2 }}>
            {((lastPlan.items ?? []) as { seq?: number; text?: string; status?: string }[]).map(
              (t, i) => (
                <div key={i} style={{ display: "flex", gap: 6, fontSize: 12 }}>
                  <span
                    style={{
                      color:
                        t.status === "done"
                          ? "var(--ant-color-success)"
                          : t.status === "doing"
                            ? "var(--ant-color-primary)"
                            : "var(--ant-color-text-quaternary)",
                    }}
                  >
                    {t.status === "done" ? "☑" : t.status === "doing" ? "◐" : "○"}
                  </span>
                  <span
                    style={{
                      textDecoration: t.status === "done" ? "line-through" : "none",
                      color: t.status === "pending" ? "var(--ant-color-text-secondary)" : undefined,
                    }}
                  >
                    {t.seq ? `${t.seq}. ` : ""}
                    {t.text}
                  </span>
                </div>
              ),
            )}
          </div>
        </div>
      )}

      {/* 事件时间线（同一次工具调用与结果已合并为一条） */}
      <div style={{ marginTop: 12 }}>
        <Typography.Text strong style={{ fontSize: 12 }}>
          执行过程（{mergedEvents.length} 条）
        </Typography.Text>
        <div style={{ marginTop: 4 }}>
          {isLoading ? (
            <Spin size="small" />
          ) : (
            mergedEvents.map((e) => (
                <EventItem
                  key={e.seq}
                  event={e}
                  resolved={
                    e.event_type === "confirmation_request" &&
                    isConfirmationResolved(events, e.seq)
                  }
                />
              ))
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * 任务详情分栏（P1-8）：既可作为右栏唯一内容（无 plugin 时，带关闭按钮），
 * 也可与 plugin 结果面上下并存（带折叠箭头）。折叠态只留一条标题栏。
 */
function DetailPane({
  runId,
  collapsed = false,
  onToggleCollapse,
  onClose,
  style,
}: {
  runId: string | null;
  collapsed?: boolean;
  onToggleCollapse?: () => void;
  onClose?: () => void;
  style?: CSSProperties;
}) {
  return (
    <div
      style={{
        ...style,
        flex: collapsed ? "none" : undefined,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        borderTop: onToggleCollapse ? "1px solid var(--ant-color-border-secondary)" : undefined,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "6px 12px",
          flex: "none",
        }}
      >
        <Space size={6}>
          {onToggleCollapse && (
            <Tooltip title={collapsed ? "展开任务详情" : "折叠为标题栏"}>
              <Button
                type="text"
                size="small"
                icon={collapsed ? <UpOutlined /> : <DownOutlined />}
                onClick={onToggleCollapse}
                aria-expanded={!collapsed}
                aria-label={collapsed ? "展开任务详情" : "折叠任务详情"}
              />
            </Tooltip>
          )}
          <Typography.Text strong style={{ fontSize: 13 }}>
            任务详情
          </Typography.Text>
          {runId && (
            <Typography.Text type="secondary" style={{ fontSize: 11 }} className="font-mono-tight">
              RUN · {runId.slice(0, 8)}
            </Typography.Text>
          )}
        </Space>
        {onClose && (
          <Button
            type="text"
            size="small"
            icon={<CloseOutlined />}
            onClick={onClose}
            aria-label="关闭详情面板"
          />
        )}
      </div>
      {!collapsed && (
        <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: "0 12px 12px" }}>
          {runId ? (
            <RunDetail runId={runId} />
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="发送消息后自动展示任务详情，或点击消息下方「任务详情」查看"
              style={{ marginTop: 24 }}
            />
          )}
        </div>
      )}
    </div>
  );
}

export default function ContextPanel() {
  const activeRunId = useSSEStore((s) => s.activeRunId);
  const toggleContextPanel = useUIStore((s) => s.toggleContextPanel);
  const sidebar = useUIStore((s) => s.sidebar);
  const closeSidebar = useUIStore((s) => s.closeSidebar);
  // P1-8：plugin 结果面与任务详情**并存**（上下分栏 + 分栏可拖拽/可折叠），不再互斥替换
  const detailCollapsed = useUIStore((s) => s.detailPaneCollapsed);
  const toggleDetailPane = useUIStore((s) => s.toggleDetailPane);
  const detailRatio = useUIStore((s) => s.sidebarDetailRatio);
  const setDetailRatio = useUIStore((s) => s.setSidebarDetailRatio);
  const railRef = useRef<HTMLDivElement>(null);
  const splitDrag = useRef(false);

  // 上下分栏拖拽：以右栏高度为基准反算任务详情占比（与左缘拖宽同一套 window 监听写法）
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!splitDrag.current || !railRef.current) return;
      const rect = railRef.current.getBoundingClientRect();
      if (rect.height > 0) setDetailRatio((rect.bottom - e.clientY) / rect.height);
    };
    const onUp = () => {
      splitDrag.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [setDetailRatio]);

  if (!sidebar) {
    return <DetailPane runId={activeRunId} onClose={toggleContextPanel} />;
  }

  return (
    <div
      ref={railRef}
      style={{ height: "100%", display: "flex", flexDirection: "column", minHeight: 0 }}
    >
      {/* plugin 面（唯一宿主；同时至多一个 plugin，见 store/ui.ts 的 sidebar 约束） */}
      <div style={{ flex: 1, minHeight: 0, display: "flex" }}>
        <PluginHost
          descriptor={sidebar}
          fallbackRunId={activeRunId}
          onClose={closeSidebar}
        />
      </div>
      {/* 分栏手柄：拖动改占比，双击折叠/展开任务详情 */}
      <div
        onMouseDown={() => {
          splitDrag.current = true;
          document.body.style.cursor = "row-resize";
          document.body.style.userSelect = "none";
        }}
        onDoubleClick={toggleDetailPane}
        title="拖动调整高度；双击折叠/展开任务详情"
        style={{
          height: 6,
          flex: "none",
          cursor: "row-resize",
          background: "var(--ant-color-border-secondary)",
        }}
      />
      <DetailPane
        runId={activeRunId}
        collapsed={detailCollapsed}
        onToggleCollapse={toggleDetailPane}
        style={detailCollapsed ? undefined : { height: `${Math.round(detailRatio * 100)}%` }}
      />
    </div>
  );
}
