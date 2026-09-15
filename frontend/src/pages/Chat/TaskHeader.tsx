/**
 * 会话顶部任务条（前端设计 §3.1）：显示当前主任务、进度、子任务抽屉入口。
 * 进度与子任务清单来自后端确定性计算（ADR-27），非 LLM 估算。
 */
import { useState } from "react";
import {
  Badge,
  Button,
  Drawer,
  Empty,
  List,
  Popconfirm,
  Progress,
  Space,
  Tag,
  Tooltip,
  Typography,
  message as antdMessage,
} from "antd";
import {
  CheckOutlined,
  ExclamationCircleOutlined,
  ProfileOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import type { TaskStepOut } from "@/api/types";
import {
  STEP_SOURCE_LABELS,
  STEP_STATUS_COLORS,
  STEP_STATUS_LABELS,
  TASK_STATUS_COLORS,
  TASK_STATUS_LABELS,
  progressText,
  stepKindLabel,
  stepLabel,
} from "./taskDisplay";

export default function TaskHeader({
  taskId,
  onOpenContext,
}: {
  taskId: string;
  onOpenContext?: () => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const { data: task } = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => tasksApi.get(taskId),
    refetchInterval: 15_000,
  });

  const stepMutation = useMutation({
    mutationFn: ({ stepId, status }: { stepId: string; status: string }) =>
      tasksApi.updateStep(taskId, stepId, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: () => antdMessage.error("子任务状态更新失败"),
  });

  if (!task) return null;

  const steps = task.steps ?? [];
  const mainSteps = steps.filter((s) => s.kind === "main");
  const branchSteps = steps.filter((s) => s.kind === "branch");

  const renderStep = (step: TaskStepOut) => (
    <List.Item
      style={{ padding: "8px 4px", alignItems: "flex-start" }}
      actions={[
        step.status !== "done" ? (
          <Popconfirm
            key="done"
            title="标记为已完成？"
            okText="完成"
            cancelText="取消"
            onConfirm={() => stepMutation.mutate({ stepId: step.id, status: "done" })}
          >
            <Tooltip title="人工标记完成">
              <Button type="text" size="small" icon={<CheckOutlined />} />
            </Tooltip>
          </Popconfirm>
        ) : null,
        step.status !== "skipped" && step.status !== "done" ? (
          <Tooltip key="skip" title="跳过该子任务">
            <Button
              type="text"
              size="small"
              icon={<StopOutlined />}
              onClick={() => stepMutation.mutate({ stepId: step.id, status: "skipped" })}
            />
          </Tooltip>
        ) : null,
      ].filter(Boolean)}
    >
      <div style={{ width: "100%" }}>
        <Space size={6} wrap>
          <Tag style={{ fontSize: 11, marginInlineEnd: 0 }}>{stepLabel(step)}</Tag>
          <Typography.Text
            strong={step.status === "doing" || step.status === "awaiting_user"}
            style={{ fontSize: 13 }}
          >
            {step.name}
          </Typography.Text>
          <Tag
            color={STEP_STATUS_COLORS[step.status]}
            style={{ fontSize: 11, marginInlineEnd: 0 }}
          >
            {STEP_STATUS_LABELS[step.status] ?? step.status}
          </Tag>
          <Tag style={{ fontSize: 11, marginInlineEnd: 0 }}>{stepKindLabel(step.kind)}</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            {STEP_SOURCE_LABELS[step.source] ?? step.source}
          </Typography.Text>
        </Space>
        {step.description && (
          <div style={{ fontSize: 12, color: "rgba(128,128,128,0.9)", marginTop: 2 }}>
            {step.description}
          </div>
        )}
        {step.resolution?.answer && (
          <div style={{ fontSize: 12, marginTop: 2 }}>
            <Typography.Text type="secondary">用户答复：</Typography.Text>
            {step.resolution.answer}
          </div>
        )}
      </div>
    </List.Item>
  );

  return (
    <>
      <div
        style={{
          flex: "none",
          borderBottom: "1px solid rgba(128,128,128,0.2)",
          padding: "6px 16px",
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <Space size={6} style={{ flex: "none" }}>
          <span>{task.task_type_icon || "📋"}</span>
          <Tooltip title={`主任务模板：${task.task_type_name}`}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {task.task_type_name}
            </Typography.Text>
          </Tooltip>
          <Typography.Text strong ellipsis style={{ fontSize: 13, maxWidth: 220 }}>
            {task.title}
          </Typography.Text>
        </Space>

        <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <Progress
            percent={task.progress_percent}
            size="small"
            showInfo={false}
            style={{ margin: 0, maxWidth: 220 }}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
            {progressText(task)}
          </Typography.Text>
          {task.status !== "active" && (
            <Tag color={TASK_STATUS_COLORS[task.status]} style={{ fontSize: 11 }}>
              {TASK_STATUS_LABELS[task.status] ?? task.status}
            </Tag>
          )}
        </div>

        <Space size={6} style={{ flex: "none" }}>
          {task.awaiting_confirm && (
            <Tag icon={<ExclamationCircleOutlined />} color="warning" style={{ fontSize: 11 }}>
              待你确认
            </Tag>
          )}
          <Badge count={branchSteps.length} size="small" offset={[-2, 2]}>
            <Button
              size="small"
              icon={<ProfileOutlined />}
              onClick={() => setOpen(true)}
            >
              子任务
            </Button>
          </Badge>
          {onOpenContext && (
            <Button size="small" type="text" onClick={onOpenContext}>
              详情
            </Button>
          )}
        </Space>
      </div>

      <Drawer
        title={`子任务清单（${task.progress_done}/${task.progress_total}）`}
        placement="right"
        width={420}
        open={open}
        onClose={() => setOpen(false)}
      >
        <Typography.Text strong style={{ fontSize: 13 }}>
          主线
        </Typography.Text>
        <List
          dataSource={mainSteps}
          renderItem={renderStep}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无主线子任务" /> }}
        />
        <Typography.Text strong style={{ fontSize: 13 }}>
          支线
        </Typography.Text>
        <List
          dataSource={branchSteps}
          renderItem={renderStep}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无支线子任务（执行中遇到需要你拍板的问题会自动登记）"
              />
            ),
          }}
        />
      </Drawer>
    </>
  );
}
