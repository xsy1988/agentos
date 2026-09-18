/**
 * 任务页（前端设计 §3.2）。
 * 三视图：列表 / 看板 / 时间线。
 * 待确认任务醒目入口 + 外部等待醒目入口 + 行内快捷操作。
 */
import { useState } from "react";
import { Tabs, Alert, Button, Space } from "antd";
import {
  TableOutlined,
  AppstoreOutlined,
  NodeIndexOutlined,
  ExclamationCircleOutlined,
  ClockCircleOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { runsApi } from "@/api/runs";
import { awaitsApi } from "@/api/awaits";
import RunsList from "./RunsList";
import RunsBoard from "./RunsBoard";
import RunTimeline from "./RunTimeline";

export default function RunsPage() {
  const [activeTab, setActiveTab] = useState("list");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  // 查询待确认任务
  const { data: pendingRuns = [] } = useQuery({
    queryKey: ["runs", "paused_awaiting_confirm"],
    queryFn: () => runsApi.list({ status: "paused_awaiting_confirm" }),
    refetchInterval: 10_000,
  });

  // P0-4：平台代为持有的外部等待（GET /awaits?status=waiting）
  const { data: waitingAwaits } = useQuery({
    queryKey: ["awaits", "waiting", "all"],
    queryFn: () => awaitsApi.list({ status: "waiting", limit: 100 }),
    refetchInterval: 10_000,
  });

  const pendingCount = pendingRuns.length;
  const waitingCount = waitingAwaits?.items.length ?? 0;

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      {pendingCount > 0 && (
        <Alert
          type="warning"
          banner
          icon={<ExclamationCircleOutlined />}
          showIcon
          message={`${pendingCount} 个任务等待你的确认`}
          action={
            <Button
              size="small"
              type="primary"
              onClick={() => {
                setActiveTab("board");
              }}
            >
              去处理
            </Button>
          }
          style={{ marginBottom: 8 }}
        />
      )}
      {waitingCount > 0 && (
        <Alert
          type="info"
          banner
          icon={<ClockCircleOutlined />}
          showIcon
          message={`${waitingCount} 个任务正在等待外部回调（可在时间线里查看或撤销等待）`}
          action={
            <Button
              size="small"
              onClick={() => {
                setSelectedRunId(waitingAwaits?.items[0].run_id ?? null);
                setActiveTab("timeline");
              }}
            >
              去查看
            </Button>
          }
          style={{ marginBottom: 8 }}
        />
      )}

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        style={{ flex: 1, minHeight: 0 }}
        items={[
          {
            key: "list",
            label: (
              <Space size={4}>
                <TableOutlined />
                列表
              </Space>
            ),
            children: (
              <RunsList
                onSelectRun={(id: string) => {
                  setSelectedRunId(id);
                  setActiveTab("timeline");
                }}
              />
            ),
          },
          {
            key: "board",
            label: (
              <Space size={4}>
                <AppstoreOutlined />
                看板
                {pendingCount > 0 && (
                  <span
                    style={{
                      background: "var(--ant-color-error)",
                      color: "#fff",
                      borderRadius: 8,
                      fontSize: 10,
                      padding: "0 5px",
                      lineHeight: "16px",
                    }}
                  >
                    {pendingCount}
                  </span>
                )}
              </Space>
            ),
            children: (
              <RunsBoard
                onSelectRun={(id: string) => {
                  setSelectedRunId(id);
                  setActiveTab("timeline");
                }}
              />
            ),
          },
          {
            key: "timeline",
            label: (
              <Space size={4}>
                <NodeIndexOutlined />
                时间线
              </Space>
            ),
            children: (
              <RunTimeline
                runId={selectedRunId}
                onSelectRun={setSelectedRunId}
              />
            ),
          },
        ]}
        tabBarStyle={{ marginBottom: 8 }}
      />
    </div>
  );
}
