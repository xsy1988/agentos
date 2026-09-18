/**
 * 任务列表视图（前端设计 §3.2）。
 * 按状态/触发方式筛选，行内操作：恢复/终止/查看回放/复制引用/撤销等待。
 */
import { useState } from "react";
import { Table, Tag, Select, Button, Space, Tooltip, Typography, message as antdMessage } from "antd";
import { StopOutlined, NodeIndexOutlined, DisconnectOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { runsApi } from "@/api/runs";
import { awaitsApi } from "@/api/awaits";
import CopyRefButton from "@/components/CopyRefButton";
import { runReference } from "@/utils/clipboard";
import type { RunOut } from "@/api/types";

const TERMINAL_STATUSES = ["done", "failed", "cancelled", "aborted", "timeout"];

const STATUS_CONFIG: Record<string, { color: string; label: string }> = {
  pending: { color: "default", label: "等待" },
  running: { color: "processing", label: "运行中" },
  paused_awaiting_confirm: { color: "warning", label: "待确认" },
  waiting_external: { color: "processing", label: "等待外部回调" },
  done: { color: "success", label: "完成" },
  failed: { color: "error", label: "失败" },
  cancelled: { color: "default", label: "已取消" },
  aborted: { color: "default", label: "已终止" },
  timeout: { color: "error", label: "超时" },
};

const TRIGGER_LABEL: Record<string, string> = {
  manual: "手动",
  timer: "定时",
  alarm: "闹钟",
};

export default function RunsList({
  onSelectRun,
}: {
  onSelectRun: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string | undefined>();
  const [page, setPage] = useState(1);

  const { data: runs = [], isLoading } = useQuery({
    queryKey: ["runs", "list", statusFilter],
    queryFn: () => runsApi.list({ status: statusFilter, limit: 100 }),
    refetchInterval: 10_000,
  });

  const abortMutation = useMutation({
    mutationFn: (runId: string) => runsApi.abort(runId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["runs"] }),
  });

  // 「撤销等待」（P0-4）：撤销该 run 正等待的外部回调，run 立即以结构化失败收尾；
  // 已落定（超时/回传）的等待不需要撤销，属提示而非错误
  const cancelAwaitMutation = useMutation({
    mutationFn: (runId: string) => awaitsApi.cancelWaitingForRun(runId),
    onSuccess: (outs) => {
      if (outs.length === 0) {
        antdMessage.info("该任务当前没有等待中的外部回调");
        return;
      }
      if (outs.some((o) => o.cancelled)) antdMessage.success("已撤销等待，任务将以失败收尾");
      else antdMessage.info(`等待已落定（${outs[0].status}），无需撤销`);
      queryClient.invalidateQueries({ queryKey: ["awaits"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
    onError: (err: Error) => antdMessage.error(`撤销等待失败：${err.message}`),
  });

  const columns = [
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (status: string) => {
        const cfg = STATUS_CONFIG[status] ?? { color: "default", label: status };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: "触发",
      dataIndex: "trigger",
      width: 80,
      render: (t: string) => TRIGGER_LABEL[t] ?? t,
    },
    {
      title: "输入",
      dataIndex: "input",
      ellipsis: true,
      render: (input: Record<string, unknown>) => {
        const text = (input?.text as string) ?? JSON.stringify(input ?? {});
        return (
          <Typography.Text ellipsis style={{ maxWidth: 300, fontSize: 13 }}>
            {text}
          </Typography.Text>
        );
      },
    },
    {
      title: "迭代",
      width: 80,
      render: (_: unknown, record: RunOut) => {
        const used = record.budget_used as Record<string, unknown>;
        const budget = record.budget as Record<string, unknown>;
        const iter = used?.iterations ?? 0;
        const maxIter = budget?.max_iterations ?? "?";
        return `${iter}/${maxIter}`;
      },
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 160,
      render: (t: string) => new Date(t).toLocaleString("zh-CN"),
    },
    {
      title: "操作",
      width: 150,
      render: (_: unknown, record: RunOut) => (
        <Space size={4}>
          <Tooltip title="事件回放">
            <Button
              type="text"
              size="small"
              icon={<NodeIndexOutlined />}
              onClick={() => onSelectRun(record.id)}
            />
          </Tooltip>
          <CopyRefButton text={runReference(record)} />
          {record.status === "waiting_external" ? (
            <Tooltip title="撤销等待（外部回调不再等待，任务以失败收尾）">
              <Button
                type="text"
                size="small"
                danger
                icon={<DisconnectOutlined />}
                loading={cancelAwaitMutation.isPending}
                onClick={(e) => {
                  e.stopPropagation();
                  cancelAwaitMutation.mutate(record.id);
                }}
              />
            </Tooltip>
          ) : (
            !TERMINAL_STATUSES.includes(record.status) && (
              <Tooltip title="终止">
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<StopOutlined />}
                  loading={abortMutation.isPending}
                  onClick={() => abortMutation.mutate(record.id)}
                />
              </Tooltip>
            )
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Space>
          <Select
            placeholder="筛选状态"
            allowClear
            style={{ width: 140 }}
            value={statusFilter}
            onChange={(v) => setStatusFilter(v)}
            options={Object.entries(STATUS_CONFIG).map(([key, cfg]) => ({
              value: key,
              label: cfg.label,
            }))}
          />
        </Space>
      </div>
      <Table
        rowKey="id"
        size="small"
        loading={isLoading}
        dataSource={runs}
        columns={columns}
        pagination={{
          current: page,
          onChange: setPage,
          pageSize: 20,
          showTotal: (total) => `共 ${total} 条`,
          size: "small",
        }}
        onRow={(record) => ({
          onClick: () => onSelectRun(record.id),
          style: { cursor: "pointer" },
        })}
      />
    </div>
  );
}
