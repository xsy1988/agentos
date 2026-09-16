/**
 * 事件回放视图（前端设计 §3.2 本页灵魂）。
 * 选历史 run → 逐条回放 run_events → 可调速 → 排查"它为什么这么做"。
 */
import { useState, useEffect, useRef, useCallback } from "react";
import {
  List,
  Button,
  Space,
  Select,
  Typography,
  Tag,
  Timeline,
  Collapse,
  Empty,
  Tooltip,
} from "antd";
import {
  PlayCircleOutlined,
  PauseCircleOutlined,
  StepForwardOutlined,
  StepBackwardOutlined,
  BulbOutlined,
  ToolOutlined,
  RobotOutlined,
  MessageOutlined,
  ScheduleOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { runsApi } from "@/api/runs";
import { useSSEStore } from "@/store/sse";
import type { RunEventOut } from "@/api/types";

// 事件类型 → 图标/颜色映射
const EVENT_META: Record<string, { icon: React.ReactNode; color: string }> = {
  run_status: { icon: <RobotOutlined />, color: "blue" },
  message_delta: { icon: <MessageOutlined />, color: "blue" },
  thought: { icon: <BulbOutlined />, color: "gold" },
  plan_updated: { icon: <ScheduleOutlined />, color: "cyan" },
  tool_call: { icon: <ToolOutlined />, color: "purple" },
  tool_result: { icon: <ToolOutlined />, color: "green" },
  context_assembly: { icon: <RobotOutlined />, color: "default" },
  context_compacted: { icon: <RobotOutlined />, color: "orange" },
  interrupt: { icon: <RobotOutlined />, color: "warning" },
  error: { icon: <RobotOutlined />, color: "red" },
  budget_used: { icon: <RobotOutlined />, color: "default" },
};

const SPEEDS = [
  { value: 2000, label: "0.5x" },
  { value: 1000, label: "1x" },
  { value: 500, label: "2x" },
  { value: 200, label: "5x" },
];

function EventContent({ event }: { event: RunEventOut }) {
  const { event_type, payload } = event;

  if (event_type === "message_delta") {
    return (
      <Typography.Text style={{ fontSize: 13 }}>
        流式片段: <code className="font-mono-tight">{String(payload.delta ?? "").slice(0, 80)}</code>
      </Typography.Text>
    );
  }

  if (event_type === "thought") {
    return (
      <Collapse
        size="small"
        style={{ border: "none" }}
        items={[{
          key: "1",
          label: <Typography.Text type="secondary" style={{ fontSize: 12 }}>思考过程</Typography.Text>,
          children: (
            <Typography.Paragraph
              type="secondary"
              style={{ fontSize: 13, whiteSpace: "pre-wrap", margin: 0 }}
            >
              {String(payload.text ?? payload.content ?? "")}
            </Typography.Paragraph>
          ),
        }]}
      />
    );
  }

  if (event_type === "plan_updated") {
    const tasks = (payload.tasks ?? []) as { task: string; done: boolean }[];
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {tasks.map((t, i) => (
          <div key={i} style={{ fontSize: 13, display: "flex", gap: 6 }}>
            <span>{t.done ? "☑" : "○"}</span>
            <span style={{ textDecoration: t.done ? "line-through" : "none" }}>{t.task}</span>
          </div>
        ))}
      </div>
    );
  }

  if (event_type === "tool_call" || event_type === "tool_result") {
    const name = String(payload.tool_name ?? payload.name ?? "unknown");
    const data = event_type === "tool_call" ? payload.args : payload.result;
    const dataStr = typeof data === "string" ? data : JSON.stringify(data, null, 2);
    return (
      <Collapse
        size="small"
        style={{ border: "none" }}
        items={[{
          key: "1",
          label: (
            <Typography.Text className="font-mono-tight" style={{ fontSize: 12 }}>
              {name} {event_type === "tool_result" ? "· 结果" : ""}
            </Typography.Text>
          ),
          children: (
            <pre className="font-mono-tight" style={{ fontSize: 12, margin: 0, whiteSpace: "pre-wrap", maxHeight: 200, overflow: "auto" }}>
              {dataStr.slice(0, 2000)}
            </pre>
          ),
        }]}
      />
    );
  }

  if (event_type === "run_status") {
    return (
      <Tag color={
        payload.status === "done" ? "success" :
        payload.status === "failed" || payload.status === "timeout" ? "error" :
        "processing"
      }>
        运行状态: {String(payload.status)}
      </Tag>
    );
  }

  if (event_type === "interrupt") {
    return (
      <Tag color="warning">中断: {String(payload.kind ?? "unknown")}</Tag>
    );
  }

  if (event_type === "error") {
    return <Tag color="error">错误: {String(payload.message ?? payload.error ?? "")}</Tag>;
  }

  // 默认：JSON 摘要
  return (
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      {JSON.stringify(payload).slice(0, 120)}
    </Typography.Text>
  );
}

export default function RunTimeline({
  runId,
  onSelectRun,
}: {
  runId: string | null;
  onSelectRun: (id: string) => void;
}) {
  const { eventsByRun } = useSSEStore();
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1000);
  const [visibleCount, setVisibleCount] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 从 API 获取事件（非 SSE，用于历史回放）
  const { data: apiEvents = [], isLoading } = useQuery({
    queryKey: ["run-events", runId],
    queryFn: () => (runId ? runsApi.events(runId) : Promise.resolve([])),
    enabled: !!runId,
  });

  // 合并 API 事件和 SSE 缓存（SSE 可能多了一些实时事件）
  const sseEvents = runId ? eventsByRun[runId] ?? [] : [];
  const allEvents = apiEvents.length >= sseEvents.length ? apiEvents : sseEvents;

  // 重置可见数
  useEffect(() => {
    setVisibleCount(0);
    setPlaying(false);
  }, [runId]);

  // 播放逻辑
  useEffect(() => {
    if (!playing) {
      if (timerRef.current) clearInterval(timerRef.current);
      return;
    }
    if (visibleCount >= allEvents.length) {
      setPlaying(false);
      return;
    }
    timerRef.current = setInterval(() => {
      setVisibleCount((c) => {
        if (c >= allEvents.length) {
          setPlaying(false);
          return c;
        }
        return c + 1;
      });
    }, speed);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [playing, speed, visibleCount, allEvents.length]);

  const visibleEvents = allEvents.slice(0, visibleCount);

  const stepForward = useCallback(() => {
    setVisibleCount((c) => Math.min(c + 1, allEvents.length));
  }, [allEvents.length]);

  const stepBackward = useCallback(() => {
    setVisibleCount((c) => Math.max(c - 1, 0));
  }, []);

  // 最近 run 列表（供选择）
  const { data: recentRuns = [] } = useQuery({
    queryKey: ["runs", "recent"],
    queryFn: () => runsApi.list({ limit: 20 }),
  });

  if (!runId) {
    return (
      <div style={{ display: "flex", gap: 12, height: "100%" }}>
        <div style={{ width: 280, overflow: "auto" }}>
          <Typography.Text type="secondary" style={{ fontSize: 12, padding: "0 8px" }}>
            选择任务回放
          </Typography.Text>
          <List
            size="small"
            dataSource={recentRuns}
            renderItem={(run) => (
              <List.Item
                style={{ cursor: "pointer", padding: "8px 12px" }}
                onClick={() => onSelectRun(run.id)}
              >
                <div style={{ width: "100%" }}>
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <Typography.Text className="font-mono-tight" style={{ fontSize: 11 }} type="secondary">
                      {run.id.slice(0, 8)}
                    </Typography.Text>
                    <Tag style={{ fontSize: 10, margin: 0 }}>{run.status}</Tag>
                  </div>
                  <Typography.Text ellipsis style={{ fontSize: 13 }}>
                    {String((run.input as Record<string, unknown>)?.text ?? "")}
                  </Typography.Text>
                </div>
              </List.Item>
            )}
          />
        </div>
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Empty description="选择一个任务查看事件回放" />
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", gap: 12, height: "100%" }}>
      {/* 左侧：run 选择列表 */}
      <div style={{ width: 240, overflow: "auto", borderRight: "1px solid var(--ant-color-border-secondary)" }}>
        <List
          size="small"
          dataSource={recentRuns}
          renderItem={(run) => (
            <List.Item
              style={{
                cursor: "pointer",
                padding: "8px 12px",
                background: run.id === runId ? "rgba(22,119,255,0.1)" : undefined,
              }}
              onClick={() => onSelectRun(run.id)}
            >
              <div style={{ width: "100%" }}>
                <Typography.Text className="font-mono-tight" style={{ fontSize: 11 }} type="secondary">
                  {run.id.slice(0, 8)}
                </Typography.Text>
                <div>
                  <Typography.Text ellipsis style={{ fontSize: 12 }}>
                    {String((run.input as Record<string, unknown>)?.text ?? "")}
                  </Typography.Text>
                </div>
              </div>
            </List.Item>
          )}
        />
      </div>

      {/* 右侧：事件回放 */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {/* 回放控制条 */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "4px 8px",
            borderBottom: "1px solid var(--ant-color-border-secondary)",
          }}
        >
          <Space size={4}>
            <Tooltip title="后退一步">
              <Button
                type="text"
                size="small"
                icon={<StepBackwardOutlined />}
                onClick={stepBackward}
                disabled={visibleCount === 0}
              />
            </Tooltip>
            <Button
              type={playing ? "primary" : "text"}
              size="small"
              icon={playing ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
              onClick={() => {
                if (visibleCount >= allEvents.length) setVisibleCount(0);
                setPlaying((p) => !p);
              }}
            >
              {playing ? "暂停" : "播放"}
            </Button>
            <Tooltip title="前进一步">
              <Button
                type="text"
                size="small"
                icon={<StepForwardOutlined />}
                onClick={stepForward}
                disabled={visibleCount >= allEvents.length}
              />
            </Tooltip>
          </Space>
          <Select
            size="small"
            value={speed}
            onChange={setSpeed}
            style={{ width: 80 }}
            options={SPEEDS}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {visibleCount}/{allEvents.length} 事件
          </Typography.Text>
        </div>

        {/* 事件列表 */}
        <div style={{ flex: 1, overflow: "auto", padding: "8px 16px" }}>
          {isLoading ? (
            <div style={{ textAlign: "center", padding: 24 }}>
              <Typography.Text type="secondary">加载中…</Typography.Text>
            </div>
          ) : visibleEvents.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="点击播放或逐步前进"
              style={{ marginTop: 48 }}
            />
          ) : (
            <Timeline
              items={visibleEvents.map((event) => {
                const meta = EVENT_META[event.event_type] ?? {
                  icon: <RobotOutlined />,
                  color: "gray",
                };
                return {
                  key: event.seq,
                  dot: (
                    <span style={{ color: `var(--ant-color-${meta.color === "gray" ? "text-secondary" : meta.color})` }}>
                      {meta.icon}
                    </span>
                  ),
                  children: (
                    <div>
                      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 2 }}>
                        <Tag style={{ fontSize: 10, margin: 0 }}>{event.event_type}</Tag>
                        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                          seq {event.seq} · {new Date(event.created_at).toLocaleTimeString("zh-CN")}
                        </Typography.Text>
                      </div>
                      <EventContent event={event} />
                    </div>
                  ),
                };
              })}
            />
          )}
        </div>
      </div>
    </div>
  );
}
