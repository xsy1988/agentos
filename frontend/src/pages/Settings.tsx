/**
 * 设置页（前端设计 §3.5）。
 * Agent 管理（人格/预算/模型绑定）+ 模型接入（LLM/Embedding，密钥只进不出）。
 */
import { useState } from "react";
import {
  Tabs,
  Table,
  Button,
  Modal,
  Form,
  Input,
  InputNumber,
  Select,
  Tag,
  Space,
  Typography,
  message,
  Popconfirm,
  Switch,
  Tooltip,
} from "antd";
import {
  PlusOutlined,
  RobotOutlined,
  CloudServerOutlined,
  DeleteOutlined,
  ApiOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { agentsApi } from "@/api/agents";
import { modelsApi } from "@/api/models";
import type { AgentOut, ModelProviderOut } from "@/api/types";

// ---------- Agent 管理子页 ----------

function AgentsTab() {
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<AgentOut | null>(null);
  const [form] = Form.useForm();

  const { data: agents = [], isLoading } = useQuery({
    queryKey: ["agents", "all"],
    queryFn: () => agentsApi.list(true),
  });

  const { data: llmModels = [] } = useQuery({
    queryKey: ["models", "llm"],
    queryFn: () => modelsApi.list("llm"),
  });

  const saveMutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const body = {
        name: values.name as string,
        description: (values.description as string) || "",
        soul_md: (values.soul_md as string) || "",
        identity_md: (values.identity_md as string) || "",
        system_prompt: (values.system_prompt as string) || "",
        model_provider_id: values.model_provider_id || null,
        tool_budget: values.tool_budget,
        max_iterations: values.max_iterations,
      };
      if (editing) return agentsApi.update(editing.id, body);
      return agentsApi.create(body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      message.success(editing ? "Agent 已更新" : "Agent 已创建");
      setModalOpen(false);
      setEditing(null);
      form.resetFields();
    },
    onError: () => message.error("保存失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => agentsApi.del(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      message.success("已删除");
    },
    onError: () => message.error("删除失败（默认 Agent 不可删除）"),
  });

  const toggleStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      agentsApi.update(id, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agents"] }),
  });

  const openEdit = (agent: AgentOut) => {
    setEditing(agent);
    form.setFieldsValue({
      name: agent.name,
      description: agent.description,
      soul_md: agent.soul_md,
      identity_md: agent.identity_md,
      system_prompt: agent.system_prompt,
      model_provider_id: agent.model_provider_id,
      tool_budget: agent.tool_budget,
      max_iterations: agent.max_iterations,
    });
    setModalOpen(true);
  };

  return (
    <div>
      <div style={{ marginBottom: 12, textAlign: "right" }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null);
            form.resetFields();
            setModalOpen(true);
          }}
        >
          新建 Agent
        </Button>
      </div>

      <Table<AgentOut>
        rowKey="id"
        dataSource={agents}
        loading={isLoading}
        size="small"
        pagination={false}
        columns={[
          {
            title: "名称",
            dataIndex: "name",
            render: (name: string, r) => (
              <Space direction="vertical" size={0}>
                <Space size={4}>
                  <Typography.Text strong>{name}</Typography.Text>
                  {r.is_default && <Tag color="blue">默认</Tag>}
                </Space>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  {r.description}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: "模型",
            dataIndex: "model_provider_id",
            width: 140,
            render: (id: string | null) => {
              const m = llmModels.find((x) => x.id === id);
              return (
                <Typography.Text style={{ fontSize: 12 }}>
                  {m?.name ?? "（未绑定）"}
                </Typography.Text>
              );
            },
          },
          {
            title: "预算",
            width: 130,
            render: (_, r) => (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {r.tool_budget} 工具 / {r.max_iterations} 轮
              </Typography.Text>
            ),
          },
          {
            title: "启用",
            dataIndex: "status",
            width: 60,
            render: (status: string, r) => (
              <Switch
                size="small"
                checked={status === "enabled"}
                disabled={r.is_default}
                onChange={(checked) =>
                  toggleStatus.mutate({ id: r.id, status: checked ? "enabled" : "disabled" })
                }
              />
            ),
          },
          {
            title: "",
            width: 90,
            render: (_, r) => (
              <Space>
                <Button size="small" type="link" onClick={() => openEdit(r)}>编辑</Button>
                {!r.is_default && (
                  <Popconfirm title="删除此 Agent？" onConfirm={() => deleteMutation.mutate(r.id)}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                )}
              </Space>
            ),
          },
        ]}
      />

      <Modal
        title={editing ? `编辑 Agent: ${editing.name}` : "新建 Agent"}
        open={modalOpen}
        onCancel={() => { setModalOpen(false); setEditing(null); }}
        onOk={() => form.validateFields().then((v) => saveMutation.mutate(v))}
        confirmLoading={saveMutation.isPending}
        width={640}
        destroyOnClose={!editing}
      >
        <Form form={form} layout="vertical" initialValues={{ tool_budget: 8, max_iterations: 25 }}>
          <Space size="small" style={{ width: "100%" }}>
            <Form.Item name="name" label="名称" rules={[{ required: true }]} style={{ width: 200 }}>
              <Input />
            </Form.Item>
            <Form.Item name="model_provider_id" label="绑定模型" style={{ width: 260 }}>
              <Select
                placeholder="选择 LLM"
                allowClear
                options={llmModels.map((m) => ({ value: m.id, label: `${m.name} (${m.model_name})` }))}
              />
            </Form.Item>
          </Space>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="一句话描述" />
          </Form.Item>
          <Space size="small">
            <Form.Item name="tool_budget" label="工具预算" rules={[{ required: true }]}>
              <InputNumber min={1} max={64} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item name="max_iterations" label="最大迭代轮数" rules={[{ required: true }]}>
              <InputNumber min={1} max={200} style={{ width: 140 }} />
            </Form.Item>
          </Space>
          <Form.Item name="soul_md" label="灵魂（Soul.md）">
            <Input.TextArea rows={4} style={{ fontFamily: "monospace", fontSize: 12 }} placeholder="价值观与原则" />
          </Form.Item>
          <Form.Item name="identity_md" label="身份（Identity.md）">
            <Input.TextArea rows={4} style={{ fontFamily: "monospace", fontSize: 12 }} placeholder="我是谁" />
          </Form.Item>
          <Form.Item name="system_prompt" label="系统提示词">
            <Input.TextArea rows={4} style={{ fontFamily: "monospace", fontSize: 12 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

// ---------- 模型接入子页 ----------

function ModelsTab() {
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ModelProviderOut | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, string>>({});
  const [form] = Form.useForm();

  const { data: models = [], isLoading } = useQuery({
    queryKey: ["models", "all"],
    queryFn: () => modelsApi.list(),
  });

  const saveMutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const body: Record<string, unknown> = {
        kind: values.kind,
        name: values.name,
        impl: values.impl,
        base_url: values.base_url,
        model_name: values.model_name,
      };
      if (values.api_key) body.api_key = values.api_key;
      // 能力标记（vision：图片附件直传或占位降级；lightweight：轻量模型，
      // 引擎的意图分类/规划/验收/闲聊回复用它降本）；保留已有 params 其余键
      const prevParams = (editing?.params as Record<string, unknown> | undefined) ?? {};
      const params = { ...prevParams, vision: !!values.vision, lightweight: !!values.lightweight };
      if (Object.keys(prevParams).length > 0 || values.vision || values.lightweight) {
        body.params = params;
      }
      if (editing) return modelsApi.update(editing.id, body);
      return modelsApi.create(body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["models"] });
      message.success(editing ? "模型已更新" : "模型已添加");
      setModalOpen(false);
      setEditing(null);
      form.resetFields();
    },
    onError: () => message.error("保存失败（检查必填字段）"),
  });

  const testMutation = useMutation({
    mutationFn: (id: string) => modelsApi.test(id),
    onMutate: (id) => setTestingId(id),
    onSuccess: (_, id) => {
      setTestResults((prev) => ({ ...prev, [id]: "✓ 连通正常" }));
      queryClient.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error, id) => {
      setTestResults((prev) => ({ ...prev, [id]: `✗ ${e.message}` }));
    },
    onSettled: () => setTestingId(null),
  });

  // 从 LLM_Gateway 同步：既有模型迁移为网关路由，新模型自动创建
  const syncMutation = useMutation({
    mutationFn: () => modelsApi.syncGateway(),
    onSuccess: (r: { total: number; migrated: number; created: number }) => {
      queryClient.invalidateQueries({ queryKey: ["models"] });
      message.success(
        `网关同步完成：共 ${r.total} 个模型，迁移 ${r.migrated} 条，新建 ${r.created} 条`,
      );
    },
    onError: (e: Error) => message.error(`同步失败：${e.message}`),
  });

  const openEdit = (m: ModelProviderOut) => {
    setEditing(m);
    form.setFieldsValue({
      kind: m.kind,
      name: m.name,
      impl: m.impl,
      base_url: m.base_url,
      model_name: m.model_name,
      api_key: "",
      vision: !!(m.params as Record<string, unknown> | undefined)?.vision,
      lightweight: !!(m.params as Record<string, unknown> | undefined)?.lightweight,
    });
    setModalOpen(true);
  };

  return (
    <div>
      <div style={{ marginBottom: 12, textAlign: "right" }}>
        <Space>
          <Tooltip title="从 LLM_Gateway 拉取模型清单：既有模型迁移为网关路由，新模型自动创建（模型统一由网关管理）">
            <Button
              icon={<SyncOutlined spin={syncMutation.isPending} />}
              loading={syncMutation.isPending}
              onClick={() => syncMutation.mutate()}
            >
              从网关同步
            </Button>
          </Tooltip>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setEditing(null);
              form.resetFields();
              setModalOpen(true);
            }}
          >
            添加模型
          </Button>
        </Space>
      </div>

      <Table<ModelProviderOut>
        rowKey="id"
        dataSource={models}
        loading={isLoading}
        size="small"
        pagination={false}
        columns={[
          {
            title: "名称",
            dataIndex: "name",
            render: (name: string, r) => (
              <Space direction="vertical" size={0}>
                <Space size={4}>
                  <Typography.Text strong>{name}</Typography.Text>
                  <Tag color={r.kind === "llm" ? "geekblue" : "cyan"}>{r.kind}</Tag>
                  {r.has_api_key && <Tag style={{ fontSize: 10 }}>密钥已配置</Tag>}
                </Space>
                <Typography.Text type="secondary" style={{ fontSize: 11, fontFamily: "monospace" }}>
                  {r.impl} · {r.model_name}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: "Base URL",
            dataIndex: "base_url",
            ellipsis: true,
            render: (url: string) => (
              <Typography.Text type="secondary" style={{ fontSize: 11, fontFamily: "monospace" }}>
                {url}
              </Typography.Text>
            ),
          },
          {
            title: "状态",
            dataIndex: "status",
            width: 70,
            render: (status: string) => (
              <Tag color={status === "enabled" ? "success" : "default"}>
                {status === "enabled" ? "启用" : "禁用"}
              </Tag>
            ),
          },
          {
            title: "连通测试",
            width: 100,
            render: (_, r) => (
              <Tooltip title={testResults[r.id]}>
                <Button
                  size="small"
                  icon={<ApiOutlined />}
                  loading={testingId === r.id}
                  onClick={() => testMutation.mutate(r.id)}
                >
                  测试
                </Button>
              </Tooltip>
            ),
          },
          {
            title: "",
            width: 70,
            render: (_, r) => (
              <Button size="small" type="link" onClick={() => openEdit(r)}>编辑</Button>
            ),
          },
        ]}
      />

      <Modal
        title={editing ? `编辑模型: ${editing.name}` : "添加模型"}
        open={modalOpen}
        onCancel={() => { setModalOpen(false); setEditing(null); }}
        onOk={() => form.validateFields().then((v) => saveMutation.mutate(v))}
        confirmLoading={saveMutation.isPending}
        destroyOnClose={!editing}
      >
        <Form form={form} layout="vertical" initialValues={{ kind: "llm", impl: "openai_compatible" }}>
          <Space size="small" style={{ width: "100%" }}>
            <Form.Item name="kind" label="类型" rules={[{ required: true }]} style={{ width: 130 }}>
              <Select options={[{ value: "llm", label: "LLM" }, { value: "embedding", label: "Embedding" }]} />
            </Form.Item>
            <Form.Item name="impl" label="实现" rules={[{ required: true }]} style={{ width: 200 }}>
              <Select
                options={[
                  { value: "openai_compatible", label: "OpenAI 兼容" },
                  { value: "anthropic", label: "Anthropic" },
                  { value: "ollama", label: "Ollama" },
                ]}
              />
            </Form.Item>
          </Space>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如: kimi" />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}>
            <Input placeholder="https://..." style={{ fontFamily: "monospace" }} />
          </Form.Item>
          <Form.Item name="model_name" label="模型名" rules={[{ required: true }]}>
            <Input placeholder="如: kimi-latest" style={{ fontFamily: "monospace" }} />
          </Form.Item>
          <Form.Item
            name="api_key"
            label={editing && editing.has_api_key ? "API Key（留空则保持不变）" : "API Key"}
          >
            <Input.Password placeholder="sk-..." autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="vision"
            label="支持视觉（图片输入）"
            initialValue={false}
            tooltip="勾选后对话中的图片附件将以多模态方式直接投喂该模型；不支持的模型会自动降级为占位文本"
          >
            <Select
              style={{ width: 200 }}
              options={[
                { value: true, label: "支持（多模态投喂）" },
                { value: false, label: "不支持（纯文本）" },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="lightweight"
            label="轻量模型（内部短调用）"
            initialValue={false}
            tooltip="勾选后引擎的意图分类/任务规划/验收与闲聊回复改用该模型（如本地小模型），降低固定 token 开销；全局只需标记一个"
          >
            <Select
              style={{ width: 200 }}
              options={[
                { value: true, label: "是（闲聊/分类/规划用）" },
                { value: false, label: "否" },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <div>
      <Typography.Title level={5} style={{ marginBottom: 12 }}>
        <CloudServerOutlined /> 设置
      </Typography.Title>
      <Tabs
        items={[
          {
            key: "agents",
            label: (
              <span>
                <RobotOutlined /> Agent 管理
              </span>
            ),
            children: <AgentsTab />,
          },
          {
            key: "models",
            label: (
              <span>
                <CloudServerOutlined /> 模型接入
              </span>
            ),
            children: <ModelsTab />,
          },
        ]}
      />
    </div>
  );
}
