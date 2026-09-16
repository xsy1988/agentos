/**
 * 任务看板视图（前端设计 §3.2）。
 * 三列：进行中 / 待确认 / 今日完成。
 */
import { Card, Tag, Typography, Empty, Spin } from "antd";
import { useQuery } from "@tanstack/react-query";
import { runsApi } from "@/api/runs";
import type { RunOut } from "@/api/types";

function isToday(dateStr: string): boolean {
  const d = new Date(dateStr);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

function RunCard({ run, onClick }: { run: RunOut; onClick: () => void }) {
  const input = run.input as Record<string, unknown>;
  const inputText = (input?.text as string) ?? JSON.stringify(input ?? {});
  const triggerLabel = run.trigger === "timer" ? "定时" : run.trigger === "alarm" ? "闹钟" : "手动";

  return (
    <Card
      size="small"
      hoverable
      onClick={onClick}
      style={{ marginBottom: 8, cursor: "pointer" }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <Typography.Text className="font-mono-tight" style={{ fontSize: 11 }} type="secondary">
          {run.id.slice(0, 8)}
        </Typography.Text>
        <Tag style={{ fontSize: 10, margin: 0 }}>{triggerLabel}</Tag>
      </div>
      <Typography.Paragraph
        ellipsis={{ rows: 2 }}
        style={{ margin: 0, fontSize: 13 }}
      >
        {inputText}
      </Typography.Paragraph>
      <Typography.Text type="secondary" style={{ fontSize: 11 }}>
        {new Date(run.created_at).toLocaleTimeString("zh-CN")}
      </Typography.Text>
    </Card>
  );
}

export default function RunsBoard({
  onSelectRun,
}: {
  onSelectRun: (id: string) => void;
}) {
  const { data: allRuns = [], isLoading } = useQuery({
    queryKey: ["runs", "board"],
    queryFn: () => runsApi.list({ limit: 100 }),
    refetchInterval: 10_000,
  });

  const running = allRuns.filter((r) => ["pending", "running"].includes(r.status));
  const pending = allRuns.filter((r) => r.status === "paused_awaiting_confirm");
  const doneToday = allRuns.filter(
    (r) =>
      ["done", "failed", "cancelled", "aborted", "timeout"].includes(r.status) &&
      isToday(r.created_at),
  );

  const columns = [
    { title: "进行中", items: running, color: "processing" },
    { title: "待确认", items: pending, color: "warning" },
    { title: "今日完成", items: doneToday, color: "success" },
  ];

  if (isLoading) {
    return (
      <div style={{ textAlign: "center", padding: 48 }}>
        <Spin />
      </div>
    );
  }

  return (
    <div style={{ display: "flex", gap: 12, height: "100%", overflow: "hidden" }}>
      {columns.map((col) => (
        <div
          key={col.title}
          style={{
            flex: 1,
            background: "var(--ant-color-bg-container)",
            borderRadius: 6,
            padding: 8,
            overflow: "auto",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              marginBottom: 8,
              paddingBottom: 6,
              borderBottom: "1px solid var(--ant-color-border-secondary)",
            }}
          >
            <Tag color={col.color}>{col.items.length}</Tag>
            <Typography.Text strong>{col.title}</Typography.Text>
          </div>
          {col.items.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              style={{ margin: "24px 0" }}
            />
          ) : (
            col.items.map((run) => (
              <RunCard
                key={run.id}
                run={run}
                onClick={() => onSelectRun(run.id)}
              />
            ))
          )}
        </div>
      ))}
    </div>
  );
}
