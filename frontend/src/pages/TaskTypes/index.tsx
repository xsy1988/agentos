/**
 * 任务模板管理页（前端设计 §3.x，ADR-23：主任务由人工定义，LLM 不自创）。
 *
 * 列表 = 主任务模板（名称/子任务数/归属能力数/实例数/启用）；
 * 右侧 Drawer = 基本信息 + 子任务模板（可排序、主线/支线）+ 归属能力。
 */
import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Drawer,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message as antdMessage,
} from "antd";
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnsType } from "antd/es/table";
import { taskTypesApi } from "@/api/tasks";
import { capabilitiesApi } from "@/api/capabilities";
import type { CapabilityOut, StepTemplateIn, TaskTypeOut } from "@/api/types";

const EMPTY_TEMPLATE = {
  name: "",
  description: "",
  playbook: "",
  icon: "📋",
  color: "",
  sort_order: 0,
  enabled: true,
};

export default function TaskTypesPage() {
  const queryClient = useQueryClient();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const { data: templates = [], isLoading } = useQuery({
    queryKey: ["task-types"],
    queryFn: taskTypesApi.list,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["task-types"] });

  const delMutation = useMutation({
    mutationFn: (id: string) => taskTypesApi.del(id),
    onSuccess: () => {
      invalidate();
      antdMessage.success("模板已删除");
    },
    onError: (e: Error) => antdMessage.error(e.message || "删除失败"),
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      taskTypesApi.update(id, { enabled }),
    onSuccess: invalidate,
    onError: (e: Error) => antdMessage.error(e.message || "更新失败"),
  });

  const columns: ColumnsType<TaskTypeOut> = [
    {
      title: "主任务",
      dataIndex: "name",
      render: (_, t) => (
        <Space size={6} align="start">
          <span>{t.icon || "📋"}</span>
          <div>
            <div>
              <Typography.Text strong>{t.name}</Typography.Text>
              {t.kind === "common" && (
                <Tag style={{ marginLeft: 6, fontSize: 11 }}>通用任务集</Tag>
              )}
            </div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {t.description || "（无描述）"}
            </Typography.Text>
          </div>
        </Space>
      ),
    },
    {
      title: "子任务",
      dataIndex: "steps",
      width: 120,
      render: (steps: TaskTypeOut["steps"]) => (
        <Space size={4} wrap>
          <Tag style={{ fontSize: 11 }}>
            主线 {steps.filter((s) => s.kind === "main").length}
          </Tag>
          <Tag style={{ fontSize: 11 }}>
            支线 {steps.filter((s) => s.kind === "branch").length}
          </Tag>
        </Space>
      ),
    },
    { title: "归属能力", dataIndex: "capability_count", width: 90 },
    { title: "任务实例", dataIndex: "task_count", width: 90 },
    { title: "排序", dataIndex: "sort_order", width: 70 },
    {
      title: "启用",
      dataIndex: "enabled",
      width: 80,
      render: (v: boolean, t) => (
        <Switch
          size="small"
          checked={v}
          disabled={t.kind === "common"}
          onChange={(checked) => toggleMutation.mutate({ id: t.id, enabled: checked })}
        />
      ),
    },
    {
      title: "操作",
      width: 150,
      render: (_, t) => (
        <Space size={4}>
          <Button size="small" onClick={() => setEditingId(t.id)}>
            编辑
          </Button>
          <Popconfirm
            title="删除该模板？"
            description="已有任务实例使用该模板时不可删除（请先停用）。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => delMutation.mutate(t.id)}
          >
            <Button size="small" danger disabled={t.kind === "common"}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: 16, overflow: "auto", height: "100%" }}>
      <div style={{ display: "flex", alignItems: "flex-start", marginBottom: 12 }}>
        <div style={{ flex: 1 }}>
          <Typography.Title level={4} style={{ margin: 0 }}>
            主任务模板
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            主任务 = 任务集合，定义主线与常见支线子任务、以及归属的能力域。Agent
            执行任务时按模板实例化子任务骨架并据此计算进度。
          </Typography.Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
          新建主任务
        </Button>
      </div>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        description="一个主任务对应一个独立会话；「通用任务集」是内建兜底域，任何会话都可能用到它的能力，不可删除。"
      />

      <Table
        rowKey="id"
        size="small"
        loading={isLoading}
        dataSource={templates}
        columns={columns}
        pagination={false}
      />

      {(creating || editingId) && (
        <TaskTypeDrawer
          taskType={templates.find((t) => t.id === editingId) ?? null}
          onClose={() => {
            setCreating(false);
            setEditingId(null);
          }}
          onSaved={invalidate}
        />
      )}
    </div>
  );
}

/** 模板编辑 Drawer：基本信息 + 子任务模板 + 归属能力 */
function TaskTypeDrawer({
  taskType,
  onClose,
  onSaved,
}: {
  taskType: TaskTypeOut | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const queryClient = useQueryClient();
  const isCommon = taskType?.kind === "common";
  const [basic, setBasic] = useState({
    name: taskType?.name ?? EMPTY_TEMPLATE.name,
    description: taskType?.description ?? EMPTY_TEMPLATE.description,
    playbook: taskType?.playbook ?? EMPTY_TEMPLATE.playbook,
    icon: taskType?.icon ?? EMPTY_TEMPLATE.icon,
    color: taskType?.color ?? EMPTY_TEMPLATE.color,
    sort_order: taskType?.sort_order ?? EMPTY_TEMPLATE.sort_order,
    enabled: taskType?.enabled ?? EMPTY_TEMPLATE.enabled,
  });
  const [steps, setSteps] = useState<StepTemplateIn[]>(
    (taskType?.steps ?? []).map((s) => ({
      name: s.name,
      description: s.description,
      playbook: s.playbook ?? "",
      references: s.references ?? null,
      kind: s.kind,
      optional: s.optional,
      capability_hint: s.capability_hint ?? null,
    })),
  );

  const { data: capabilities = [] } = useQuery({
    queryKey: ["capabilities", "all"],
    queryFn: () => capabilitiesApi.list(true),
  });
  const { data: bound = [] } = useQuery({
    queryKey: ["task-types", taskType?.id, "capabilities"],
    queryFn: () => taskTypesApi.capabilities(taskType!.id),
    enabled: !!taskType,
  });

  const boundIds = useMemo(() => bound.map((b) => b.capability_id), [bound]);

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!taskType) {
        return taskTypesApi.create({ ...basic, steps });
      }
      await taskTypesApi.update(taskType.id, basic);
      await taskTypesApi.replaceSteps(taskType.id, steps);
      return taskType.id;
    },
    onSuccess: () => {
      onSaved();
      antdMessage.success("已保存");
      onClose();
    },
    onError: (e: Error) => antdMessage.error(e.message || "保存失败"),
  });

  const bindMutation = useMutation({
    mutationFn: (ids: string[]) => taskTypesApi.bindCapabilities(taskType!.id, ids),
    onSuccess: () => {
      invalidateCapabilities();
      onSaved();
    },
    onError: (e: Error) => antdMessage.error(e.message || "归属失败"),
  });

  const unbindMutation = useMutation({
    mutationFn: (capabilityId: string) =>
      taskTypesApi.unbindCapability(taskType!.id, capabilityId),
    onSuccess: () => {
      invalidateCapabilities();
      onSaved();
    },
    onError: (e: Error) => antdMessage.error(e.message || "解除归属失败"),
  });

  function invalidateCapabilities() {
    queryClient.invalidateQueries({
      queryKey: ["task-types", taskType?.id, "capabilities"],
    });
  }

  useEffect(() => {
    if (!taskType) return;
    setBasic({
      name: taskType.name,
      description: taskType.description,
      playbook: taskType.playbook ?? "",
      icon: taskType.icon ?? EMPTY_TEMPLATE.icon,
      color: taskType.color ?? EMPTY_TEMPLATE.color,
      sort_order: taskType.sort_order,
      enabled: taskType.enabled,
    });
    setSteps(
      taskType.steps.map((s) => ({
        name: s.name,
        description: s.description,
        playbook: s.playbook ?? "",
        references: s.references ?? null,
        kind: s.kind,
        optional: s.optional,
        capability_hint: s.capability_hint ?? null,
      })),
    );
  }, [taskType]);

  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= steps.length) return;
    const next = [...steps];
    [next[i], next[j]] = [next[j], next[i]];
    setSteps(next);
  };

  return (
    <Drawer
      title={taskType ? `编辑：${taskType.name}` : "新建主任务模板"}
      width={640}
      open
      onClose={onClose}
      extra={
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button
            type="primary"
            loading={saveMutation.isPending}
            disabled={!basic.name.trim()}
            onClick={() => saveMutation.mutate()}
          >
            保存
          </Button>
        </Space>
      }
    >
      <Form layout="vertical" size="small">
        <Form.Item label="主任务名称" required>
          <Input
            value={basic.name}
            maxLength={128}
            placeholder="例如：供应商报价对比"
            onChange={(e) => setBasic({ ...basic, name: e.target.value })}
          />
        </Form.Item>
        <Form.Item label="目标说明（L1，注入任务卡，新建任务时展示）">
          <Input.TextArea
            rows={2}
            value={basic.description}
            onChange={(e) => setBasic({ ...basic, description: e.target.value })}
          />
        </Form.Item>
        <Form.Item label="执行指引 playbook（L2，Worker 激活时注入任务卡，指导 Agent 怎么干）">
          <Input.TextArea
            rows={5}
            value={basic.playbook}
            placeholder={
              "干什么（目标与产出）/ 怎么干（步骤顺序、输入输出）/ 会遇到什么问题 / 如何处理（ask_user 或 declare_subtask）/ 何时调哪个 mcp·tool·plugin"
            }
            style={{ fontFamily: "monospace", fontSize: 12 }}
            onChange={(e) => setBasic({ ...basic, playbook: e.target.value })}
          />
        </Form.Item>
        <Space size={12} align="start">
          <Form.Item label="看板图标">
            <Input
              style={{ width: 80 }}
              value={basic.icon ?? ""}
              onChange={(e) => setBasic({ ...basic, icon: e.target.value })}
            />
          </Form.Item>
          <Form.Item label="排序">
            <InputNumber
              value={basic.sort_order}
              onChange={(v) => setBasic({ ...basic, sort_order: Number(v ?? 0) })}
            />
          </Form.Item>
          <Form.Item label="启用">
            <Switch
              checked={basic.enabled}
              disabled={isCommon}
              onChange={(v) => setBasic({ ...basic, enabled: v })}
            />
          </Form.Item>
        </Space>
      </Form>

      <Typography.Text strong style={{ fontSize: 13 }}>
        子任务模板
      </Typography.Text>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
        主线子任务按序出现在每个任务实例中；支线子任务按需触发（Agent
        执行中提问时也会自动登记）。
      </Typography.Paragraph>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {steps.map((s, i) => (
          <Card key={i} size="small" styles={{ body: { padding: 8 } }}>
            <Space size={6} style={{ width: "100%" }} align="start">
              <Input
                style={{ width: 170 }}
                placeholder="子任务名称"
                value={s.name}
                onChange={(e) =>
                  setSteps(steps.map((x, k) => (k === i ? { ...x, name: e.target.value } : x)))
                }
              />
              <Select
                style={{ width: 82 }}
                value={s.kind ?? "main"}
                options={[
                  { value: "main", label: "主线" },
                  { value: "branch", label: "支线" },
                ]}
                onChange={(v) => setSteps(steps.map((x, k) => (k === i ? { ...x, kind: v } : x)))}
              />
              <Input
                style={{ width: 150 }}
                placeholder="说明（可选）"
                value={s.description ?? ""}
                onChange={(e) =>
                  setSteps(
                    steps.map((x, k) => (k === i ? { ...x, description: e.target.value } : x)),
                  )
                }
              />
              <Button
                size="small"
                type="text"
                icon={<ArrowUpOutlined />}
                disabled={i === 0}
                onClick={() => move(i, -1)}
              />
              <Button
                size="small"
                type="text"
                icon={<ArrowDownOutlined />}
                disabled={i === steps.length - 1}
                onClick={() => move(i, 1)}
              />
              <Button
                size="small"
                type="text"
                danger
                icon={<DeleteOutlined />}
                onClick={() => setSteps(steps.filter((_, k) => k !== i))}
              />
            </Space>
            <Input.TextArea
              rows={2}
              style={{ marginTop: 6, fontFamily: "monospace", fontSize: 12 }}
              placeholder="子任务 playbook（L2，可选：这一步怎么干、遇何问题、调哪个能力）"
              value={s.playbook ?? ""}
              onChange={(e) =>
                setSteps(
                  steps.map((x, k) => (k === i ? { ...x, playbook: e.target.value } : x)),
                )
              }
            />
          </Card>
        ))}
      </div>
      <Button
        size="small"
        type="dashed"
        icon={<PlusOutlined />}
        style={{ marginTop: 8 }}
        onClick={() =>
          setSteps([...steps, { name: "", kind: "main", description: "", playbook: "" }])
        }
      >
        添加子任务
      </Button>

      {!taskType && (
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 12 }}
          message="保存后可继续配置归属能力"
        />
      )}

      {taskType && (
        <>
          <Typography.Title level={5} style={{ marginTop: 24 }}>
            归属能力（能力域）
          </Typography.Title>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            执行本主任务时优先召回这些能力；域内不足时会自动回退「通用任务集」与全局，因此漏配不会
            导致能力不可用，只会降低召回优先级。
          </Typography.Paragraph>
          <Select
            mode="multiple"
            style={{ width: "100%" }}
            placeholder="选择归属能力"
            value={boundIds}
            optionFilterProp="label"
            loading={bindMutation.isPending || unbindMutation.isPending}
            options={capabilities.map((c: CapabilityOut) => ({
              value: c.id,
              label: `${c.name}（${c.type}${c.enabled ? "" : "·已停用"}）`,
            }))}
            onChange={(ids: string[]) => {
              const added = ids.filter((id) => !boundIds.includes(id));
              const removed = boundIds.filter((id) => !ids.includes(id));
              if (added.length) bindMutation.mutate(added);
              for (const id of removed) unbindMutation.mutate(id);
            }}
          />
          <Space wrap size={4} style={{ marginTop: 8 }}>
            {bound.map((b) => (
              <Tag key={b.capability_id} style={{ fontSize: 11 }}>
                {b.name}
              </Tag>
            ))}
            {bound.length === 0 && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                当前未归属任何能力（全部靠通用任务集与全局召回兜底）
              </Typography.Text>
            )}
          </Space>
        </>
      )}
    </Drawer>
  );
}
