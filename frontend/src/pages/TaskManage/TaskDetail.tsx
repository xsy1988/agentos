/**
 * 主任务详情页（/tasks/:taskId）：实例概览 + 子任务管理。
 * Worker 定义（WORKER.md 文件包）维护已拆到 /capabilities/workers，这里只读展示归属并提供跳转。
 */
import { useState } from "react";
import {
  Button,
  Card,
  Empty,
  Input,
  List,
  Popconfirm,
  Progress,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  message as antdMessage,
} from "antd";
import {
  ArrowLeftOutlined,
  CheckOutlined,
  MessageOutlined,
  PlusOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { tasksApi } from "@/api/tasks";
import type { TaskStepOut } from "@/api/types";
import {
  STEP_BLOCK_REASON_LABELS,
  STEP_CONVERGE_LABELS,
  STEP_SOURCE_LABELS,
  STEP_STATUS_COLORS,
  STEP_STATUS_LABELS,
  TASK_STATUS_COLORS,
  TASK_STATUS_LABELS,
  stepKindLabel,
} from "@/pages/Chat/taskDisplay";

const STEP_STATUS_OPTIONS = [
  "pending",
  "doing",
  "done",
  "skipped",
  "blocked",
  "awaiting_user",
].map((s) => ({ value: s, label: STEP_STATUS_LABELS[s] ?? s }));

function StepItem({
  step,
  onStatusChange,
  onConverge,
}: {
  step: TaskStepOut;
  onStatusChange: (step: TaskStepOut, status: string) => void;
  onConverge: (step: TaskStepOut, action: "close" | "requeue" | "escalate") => void;
}) {
  const resolution = step.resolution as Record<string, unknown> | null;
  return (
    <List.Item
      actions={[
        // 受阻支线：前台收敛三动作（P1-6），人工不必再直接改库
        ...(step.status === "blocked"
          ? (["close", "requeue", "escalate"] as const).map((action) => (
              <Popconfirm
                key={action}
                title={`${STEP_CONVERGE_LABELS[action]}？`}
                okText="确定"
                cancelText="取消"
                onConfirm={() => onConverge(step, action)}
              >
                <Button type="link" size="small" style={{ fontSize: 12, padding: 0 }}>
                  {STEP_CONVERGE_LABELS[action]}
                </Button>
              </Popconfirm>
            ))
          : []),
        <Select
          key="status"
          size="small"
          value={step.status}
          style={{ width: 110 }}
          options={STEP_STATUS_OPTIONS}
          onChange={(v) => onStatusChange(step, v)}
        />,
      ]}
    >
      <List.Item.Meta
        title={
          <Space size={6} wrap>
            <Typography.Text strong style={{ fontSize: 13 }}>
              {step.seq}. {step.name}
            </Typography.Text>
            <Tag
              color={STEP_STATUS_COLORS[step.status]}
              style={{ fontSize: 11 }}
            >
              {STEP_STATUS_LABELS[step.status] ?? step.status}
            </Tag>
            <Tag style={{ fontSize: 11 }}>{stepKindLabel(step.kind)}</Tag>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              {STEP_SOURCE_LABELS[step.source] ?? step.source}
            </Typography.Text>
          </Space>
        }
        description={
          <>
            {step.description && (
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, display: "block" }}
              >
                {step.description}
              </Typography.Text>
            )}
            {resolution?.question && (
              <Typography.Text
                type="warning"
                style={{ fontSize: 12, display: "block" }}
              >
                等待答复：{String(resolution.question)}
              </Typography.Text>
            )}
            {resolution?.answer && (
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, display: "block" }}
              >
                已答复：{String(resolution.answer)}
              </Typography.Text>
            )}
            {step.status === "blocked" && (
              <Typography.Text
                type="warning"
                style={{ fontSize: 12, display: "block" }}
              >
                受阻原因：
                {STEP_BLOCK_REASON_LABELS[String(resolution?.reason ?? "")] ?? "未标注"}
                {resolution?.detail ? ` · ${String(resolution.detail)}` : ""}
                {resolution?.escalated ? "（已转人工）" : ""}
              </Typography.Text>
            )}
          </>
        }
      />
    </List.Item>
  );
}

export default function TaskDetailPage() {
  const { taskId } = useParams<{ taskId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [newStep, setNewStep] = useState({ name: "", description: "", kind: "branch" });

  const { data: task, isLoading } = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => tasksApi.get(taskId!),
    enabled: !!taskId,
    refetchInterval: 15_000,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["task", taskId] });
    queryClient.invalidateQueries({ queryKey: ["tasks"] });
  };

  const statusMutation = useMutation({
    mutationFn: ({ stepId, status }: { stepId: string; status: string }) =>
      tasksApi.updateStep(taskId!, stepId, { status }),
    onSuccess: invalidate,
    onError: () => antdMessage.error("状态更新失败"),
  });

  // 受阻支线收敛（P1-6）：close / requeue / escalate
  const convergeMutation = useMutation({
    mutationFn: ({
      stepId,
      action,
    }: {
      stepId: string;
      action: "close" | "requeue" | "escalate";
    }) => tasksApi.convergeStep(taskId!, stepId, { action }),
    onSuccess: (_data, vars) => {
      invalidate();
      antdMessage.success(`${STEP_CONVERGE_LABELS[vars.action]}成功`);
    },
    onError: () => antdMessage.error("支线收敛失败（可能已被处理）"),
  });

  const addStepMutation = useMutation({
    mutationFn: () =>
      tasksApi.addStep(taskId!, {
        name: newStep.name,
        description: newStep.description,
        kind: newStep.kind,
      }),
    onSuccess: () => {
      invalidate();
      setAdding(false);
      setNewStep({ name: "", description: "", kind: "branch" });
      antdMessage.success("子任务已添加");
    },
    onError: () => antdMessage.error("添加失败"),
  });

  if (isLoading || !task) {
    return <Spin style={{ display: "block", margin: "80px auto" }} />;
  }

  const mainSteps = task.steps.filter((s) => s.kind === "main");
  const branchSteps = task.steps.filter((s) => s.kind === "branch");

  return (
    <div style={{ height: "100%", overflow: "auto", padding: "12px 16px" }}>
      {/* 头部概览 */}
      <div className="page-action-bar">
        <div>
          <Space size={8}>
            <Button
              type="text"
              icon={<ArrowLeftOutlined />}
              onClick={() => navigate("/tasks")}
            />
            <span style={{ fontSize: 18 }}>{task.worker_icon || "📋"}</span>
            <Typography.Title level={4} style={{ margin: 0 }}>
              {task.title}
            </Typography.Title>
            <Tag color={TASK_STATUS_COLORS[task.status]}>
              {TASK_STATUS_LABELS[task.status] ?? task.status}
            </Tag>
            {task.awaiting_confirm && (
              <Tag color="warning">待确认{task.awaiting_steps_count > 0 ? ` ${task.awaiting_steps_count}` : ""}</Tag>
            )}
          </Space>
          <div className="page-action-sub">
            Worker {task.worker_display_name} · 版本 {task.worker_version || "—"} · 子任务{" "}
            {task.progress_done}/{task.progress_total} · 执行 {task.run_ids.length} 次 · 更新{" "}
            {new Date(task.updated_at).toLocaleString()}
          </div>
        </div>
        <Space>
          {task.conversation && (
            <Button
              icon={<MessageOutlined />}
              onClick={() => navigate(`/chat?task=${task.id}`)}
            >
              去会话
            </Button>
          )}
          {task.worker_name && task.worker_name !== "__common__" && (
            <Button
              icon={<TeamOutlined />}
              onClick={() =>
                navigate(
                  `/capabilities/workers/${encodeURIComponent(task.worker_name)}`,
                )
              }
            >
              在 Worker 管理中打开
            </Button>
          )}
        </Space>
      </div>

      <Progress
        percent={task.progress_percent}
        size="small"
        style={{ maxWidth: 480, marginBottom: 12 }}
      />

      {/* 子任务管理 */}
      <Card
        size="small"
        title={`子任务（${task.progress_done}/${task.progress_total}）`}
        style={{ marginBottom: 12 }}
        extra={
          <Button
            size="small"
            icon={<PlusOutlined />}
            onClick={() => setAdding((v) => !v)}
          >
            添加子任务
          </Button>
        }
      >
        {adding && (
          <Space size={8} style={{ marginBottom: 12, display: "flex" }} wrap>
            <Input
              size="small"
              placeholder="子任务名称"
              style={{ width: 220 }}
              value={newStep.name}
              onChange={(e) => setNewStep((s) => ({ ...s, name: e.target.value }))}
            />
            <Input
              size="small"
              placeholder="描述（可选）"
              style={{ width: 280 }}
              value={newStep.description}
              onChange={(e) => setNewStep((s) => ({ ...s, description: e.target.value }))}
            />
            <Select
              size="small"
              value={newStep.kind}
              style={{ width: 100 }}
              options={[
                { value: "branch", label: "支线" },
                { value: "main", label: "主线" },
              ]}
              onChange={(v) => setNewStep((s) => ({ ...s, kind: v }))}
            />
            <Button
              size="small"
              type="primary"
              icon={<CheckOutlined />}
              disabled={!newStep.name.trim()}
              loading={addStepMutation.isPending}
              onClick={() => addStepMutation.mutate()}
            >
              添加
            </Button>
          </Space>
        )}

        <Typography.Text strong style={{ fontSize: 13 }}>
          主线
        </Typography.Text>
        <List
          size="small"
          dataSource={mainSteps}
          renderItem={(s) => (
            <StepItem
              step={s}
              onStatusChange={(step, status) =>
                statusMutation.mutate({ stepId: step.id, status })
              }
              onConverge={(step, action) =>
                convergeMutation.mutate({ stepId: step.id, action })
              }
            />
          )}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无主线子任务"
                style={{ margin: "8px 0" }}
              />
            ),
          }}
        />
        <Typography.Text strong style={{ fontSize: 13 }}>
          支线
        </Typography.Text>
        <List
          size="small"
          dataSource={branchSteps}
          renderItem={(s) => (
            <StepItem
              step={s}
              onStatusChange={(step, status) =>
                statusMutation.mutate({ stepId: step.id, status })
              }
              onConverge={(step, action) =>
                convergeMutation.mutate({ stepId: step.id, action })
              }
            />
          )}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无支线子任务"
                style={{ margin: "8px 0" }}
              />
            ),
          }}
        />
      </Card>
    </div>
  );
}
