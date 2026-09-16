/**
 * 任务管理（原「任务模板」，ADR-23 L1/L2 统一入口）。
 * Tab「主任务」：全部任务实例列表，点击进入 /tasks/:id 详情页
 *   （实例概览 + 子任务管理 + 所属 Worker 的 WORKER.md 编辑）；
 * Tab「Worker 模板」：主任务模板维护（新建/编辑/启停/能力归属，复用原模板页）。
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Button,
  Popconfirm,
  Progress,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message as antdMessage,
} from "antd";
import { MessageOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import type { ColumnsType } from "antd/es/table";
import { tasksApi } from "@/api/tasks";
import type { TaskOut } from "@/api/types";
import { TASK_STATUS_COLORS, TASK_STATUS_LABELS } from "@/pages/Chat/taskDisplay";
import TaskTypesPage from "@/pages/TaskTypes";

function TaskInstances() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: tasks = [], isLoading } = useQuery({
    queryKey: ["tasks", "manage"],
    queryFn: () => tasksApi.list({ limit: 500 }),
    refetchInterval: 20_000,
  });

  const delMutation = useMutation({
    mutationFn: (id: string) => tasksApi.del(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      antdMessage.success("任务已删除");
    },
    onError: () => antdMessage.error("删除失败"),
  });

  const columns: ColumnsType<TaskOut> = [
    {
      title: "主任务",
      dataIndex: "title",
      render: (_, t) => (
        <Space size={8}>
          <span style={{ fontSize: 16 }}>{t.task_type_icon || "📋"}</span>
          <div>
            <Typography.Text
              strong
              style={{ cursor: "pointer" }}
              onClick={() => navigate(`/tasks/${t.id}`)}
            >
              {t.title}
            </Typography.Text>
            <div style={{ fontSize: 12, color: "var(--ant-color-text-tertiary)" }}>
              {t.task_type_name}
            </div>
          </div>
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (s: string) => (
        <Tag color={TASK_STATUS_COLORS[s]} style={{ fontSize: 11 }}>
          {TASK_STATUS_LABELS[s] ?? s}
        </Tag>
      ),
    },
    {
      title: "进度",
      width: 170,
      render: (_, t) => (
        <Space size={6}>
          <Progress
            percent={t.progress_percent}
            size="small"
            style={{ width: 90, marginBottom: 0 }}
            showInfo={false}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t.progress_done}/{t.progress_total}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "待确认",
      dataIndex: "awaiting_confirm",
      width: 90,
      render: (v: boolean, t) =>
        v ? (
          <Tag color="warning" style={{ fontSize: 11 }}>
            待确认{t.awaiting_steps_count > 0 ? ` ${t.awaiting_steps_count}` : ""}
          </Tag>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    {
      title: "会话",
      width: 90,
      render: (_, t) =>
        t.conversation ? (
          <Button
            size="small"
            icon={<MessageOutlined />}
            onClick={() => navigate(`/chat?task=${t.id}`)}
          >
            对话
          </Button>
        ) : null,
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 160,
      render: (v: string) => new Date(v).toLocaleString(),
    },
    {
      title: "操作",
      width: 140,
      render: (_, t) => (
        <Space size={4}>
          <Button size="small" onClick={() => navigate(`/tasks/${t.id}`)}>
            详情
          </Button>
          <Popconfirm
            title="删除该任务实例？"
            description="子任务随之清除；会话与消息保留。"
            okText="删除"
            okButtonProps={{ danger: true }}
            onConfirm={() => delMutation.mutate(t.id)}
          >
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Table
      rowKey="id"
      size="middle"
      loading={isLoading}
      dataSource={tasks}
      columns={columns}
      pagination={{ pageSize: 20, showTotal: (n) => `共 ${n} 个主任务` }}
    />
  );
}

export default function TaskManagePage() {
  const [searchParams] = useSearchParams();
  // 深链 ?tab=templates（旧「任务模板」入口/命令面板跳转）直开 Worker 模板 Tab
  const [activeTab, setActiveTab] = useState(
    searchParams.get("tab") === "templates" ? "templates" : "instances",
  );
  return (
    <div style={{ height: "100%", overflow: "auto", padding: "0 4px" }}>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: "instances", label: "主任务", children: <TaskInstances /> },
          { key: "templates", label: "Worker 模板", children: <TaskTypesPage /> },
        ]}
      />
    </div>
  );
}
