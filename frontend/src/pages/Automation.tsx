/**
 * 自动化页（前端设计 §3.5）。
 * 定时任务（cron + Agent + 输入模板）与闹钟（一次性提醒）。
 */
import { useState } from "react";
import {
  Tabs,
  Table,
  Button,
  Form,
  Input,
  Select,
  Tag,
  Space,
  Typography,
  List,
  Empty,
  Popconfirm,
  message,
  DatePicker,
} from "antd";
import {
  PlusOutlined,
  ClockCircleOutlined,
  BellOutlined,
  DeleteOutlined,
  PauseOutlined,
  PlayCircleOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { schedulerApi } from "@/api/scheduler";
import { agentsApi } from "@/api/agents";
import type { TimerOut, AlarmOut } from "@/api/types";
import FormDrawer from "@/components/FormDrawer";
import dayjs from "dayjs";

function fmtTime(dt: string | null): string {
  if (!dt) return "—";
  return new Date(dt).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function TimersTab() {
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  const { data: timers = [], isLoading } = useQuery({
    queryKey: ["timers"],
    queryFn: schedulerApi.timers,
  });

  const { data: agents = [] } = useQuery({
    queryKey: ["agents", "enabled"],
    queryFn: () => agentsApi.list(),
  });

  const createMutation = useMutation({
    mutationFn: (values: { name: string; cron_expr: string; agent_id: string; prompt: string }) =>
      schedulerApi.createTimer({
        name: values.name,
        cron_expr: values.cron_expr,
        agent_id: values.agent_id,
        input_template: values.prompt ? { text: values.prompt } : {},
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["timers"] });
      message.success("定时任务已创建");
      setModalOpen(false);
      form.resetFields();
    },
    onError: () => message.error("创建失败（检查 cron 表达式与 Agent）"),
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "active" | "paused" }) =>
      schedulerApi.updateTimer(id, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["timers"] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => schedulerApi.delTimer(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["timers"] });
      message.success("已删除");
    },
  });

  return (
    <div>
      <div className="page-action-bar">
        <div>
          <Typography.Text strong>定时任务</Typography.Text>
          <div className="page-action-sub">按 cron 周期触发 Agent 执行</div>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
          新建定时任务
        </Button>
      </div>
      <Table<TimerOut>
        rowKey="id"
        dataSource={timers}
        loading={isLoading}
        size="small"
        pagination={false}
        locale={{ emptyText: <Empty description="暂无定时任务" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        columns={[
          {
            title: "名称",
            dataIndex: "name",
            render: (name: string, r) => (
              <Space direction="vertical" size={0}>
                <Typography.Text strong>{name}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 11, fontFamily: "monospace" }}>
                  {r.cron_expr}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: "Agent",
            dataIndex: "agent_id",
            width: 140,
            render: (agentId: string) => {
              const agent = agents.find((a) => a.id === agentId);
              return <Typography.Text style={{ fontSize: 12 }}>{agent?.name ?? agentId.slice(0, 8)}</Typography.Text>;
            },
          },
          {
            title: "下次触发",
            dataIndex: "next_fire_at",
            width: 120,
            render: (v: string | null) => <Typography.Text style={{ fontSize: 12 }}>{fmtTime(v)}</Typography.Text>,
          },
          {
            title: "上次触发",
            dataIndex: "last_fire_at",
            width: 120,
            render: (v: string | null) => <Typography.Text style={{ fontSize: 12 }}>{fmtTime(v)}</Typography.Text>,
          },
          {
            title: "状态",
            dataIndex: "status",
            width: 80,
            render: (status: string, r) => (
              <Space>
                <Tag color={status === "active" ? "success" : "default"}>
                  {status === "active" ? "运行中" : "已暂停"}
                </Tag>
                <Button
                  size="small"
                  type="text"
                  icon={status === "active" ? <PauseOutlined /> : <PlayCircleOutlined />}
                  onClick={() =>
                    toggleMutation.mutate({ id: r.id, status: status === "active" ? "paused" : "active" })
                  }
                />
              </Space>
            ),
          },
          {
            title: "",
            width: 50,
            render: (_, r) => (
              <Popconfirm title="删除此定时任务？" onConfirm={() => deleteMutation.mutate(r.id)}>
                <Button size="small" type="text" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            ),
          },
        ]}
      />

      <FormDrawer
        title="新建定时任务"
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onOk={() => form.validateFields().then((v) => createMutation.mutate(v))}
        confirmLoading={createMutation.isPending}
        width={520}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如: 每日晨报" />
          </Form.Item>
          <Form.Item
            name="cron_expr"
            label="Cron 表达式"
            rules={[{ required: true }]}
            extra="分 时 日 月 周（如 0 8 * * * = 每天 8:00）"
          >
            <Input placeholder="0 8 * * *" style={{ fontFamily: "monospace" }} />
          </Form.Item>
          <Form.Item name="agent_id" label="执行 Agent" rules={[{ required: true }]}>
            <Select
              placeholder="选择 Agent"
              options={agents
                .filter((a) => a.status === "enabled")
                .map((a) => ({ value: a.id, label: a.name }))}
            />
          </Form.Item>
          <Form.Item name="prompt" label="输入提示（可选）">
            <Input.TextArea rows={2} placeholder="交给 Agent 的输入文本" />
          </Form.Item>
        </Form>
      </FormDrawer>
    </div>
  );
}

function AlarmsTab() {
  const queryClient = useQueryClient();
  const [form] = Form.useForm();

  const { data: alarms = [], isLoading } = useQuery({
    queryKey: ["alarms"],
    queryFn: schedulerApi.alarms,
    refetchInterval: 30_000,
  });

  const createMutation = useMutation({
    mutationFn: (values: { content: string; fire_at: dayjs.Dayjs }) =>
      schedulerApi.createAlarm({
        content: values.content,
        fire_at: values.fire_at.toISOString(),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alarms"] });
      message.success("闹钟已设置");
      form.resetFields();
    },
    onError: () => message.error("设置失败"),
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => schedulerApi.delAlarm(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alarms"] });
      message.success("已取消");
    },
  });

  const pending = alarms.filter((a) => a.status === "pending");
  const done = alarms.filter((a) => a.status !== "pending");

  return (
    <div>
      <Form
        form={form}
        layout="inline"
        style={{ marginBottom: 16 }}
        onFinish={(v) => createMutation.mutate(v as { content: string; fire_at: dayjs.Dayjs })}
      >
        <Form.Item name="content" rules={[{ required: true }]} style={{ flex: 1, minWidth: 200 }}>
          <Input placeholder="提醒内容，如：下午 3 点开会" />
        </Form.Item>
        <Form.Item name="fire_at" rules={[{ required: true }]}>
          <DatePicker showTime style={{ width: 180 }} />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit" icon={<PlusOutlined />} loading={createMutation.isPending}>
            设置
          </Button>
        </Form.Item>
      </Form>

      <List
        loading={isLoading}
        dataSource={[...pending, ...done]}
        locale={{ emptyText: <Empty description="暂无闹钟" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        renderItem={(alarm: AlarmOut) => (
          <List.Item
            actions={
              alarm.status === "pending"
                ? [
                    <Popconfirm key="cancel" title="取消此闹钟？" onConfirm={() => cancelMutation.mutate(alarm.id)}>
                      <Button size="small" type="text" danger icon={<DeleteOutlined />}>取消</Button>
                    </Popconfirm>,
                  ]
                : []
            }
          >
            <div style={{ flex: 1 }}>
              <Space>
                <BellOutlined style={{ color: alarm.status === "pending" ? "#faad14" : undefined }} />
                <Typography.Text delete={alarm.status !== "pending"}>{alarm.content}</Typography.Text>
                <Tag color={alarm.status === "pending" ? "warning" : alarm.status === "fired" ? "success" : "default"}>
                  {alarm.status === "pending" ? "等待中" : alarm.status === "fired" ? "已触发" : alarm.status}
                </Tag>
              </Space>
              <div style={{ marginTop: 2 }}>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  触发时间: {fmtTime(alarm.fire_at)}
                </Typography.Text>
              </div>
            </div>
          </List.Item>
        )}
      />
    </div>
  );
}

export default function AutomationPage() {
  return (
    <div>
      <Typography.Title level={5} style={{ marginBottom: 12 }}>
        <ClockCircleOutlined /> 自动化
      </Typography.Title>
      <Tabs
        items={[
          { key: "timers", label: "定时任务", children: <TimersTab /> },
          { key: "alarms", label: "闹钟", children: <AlarmsTab /> },
        ]}
      />
    </div>
  );
}
