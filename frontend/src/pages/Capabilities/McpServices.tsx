/**
 * MCP 服务页（前端设计 §3.4）。
 * 注册 MCP Server → 冒烟测试 → 工具级开关 → 健康状态。
 */
import { useState } from "react";
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Radio,
  Tag,
  Switch,
  Space,
  Typography,
  message,
  Alert,
  Popconfirm,
} from "antd";
import {
  PlusOutlined,
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { capabilitiesApi, type CapabilityCreatedOut } from "@/api/capabilities";
import type { CapabilityOut, CapabilityToolOut } from "@/api/types";

const RISK_COLORS: Record<string, string> = {
  read: "green",
  write: "orange",
  dangerous: "red",
};

const HEALTH_META: Record<string, { color: string; label: string }> = {
  healthy: { color: "success", label: "健康" },
  degraded: { color: "warning", label: "降级" },
  down: { color: "error", label: "离线" },
  unknown: { color: "default", label: "未知" },
};

function ToolsCell({ capId }: { capId: string }) {
  const { data: tools = [] } = useQuery({
    queryKey: ["cap-tools", capId],
    queryFn: () => capabilitiesApi.tools(capId),
    staleTime: 30_000,
  });
  if (tools.length === 0) return <Typography.Text type="secondary">—</Typography.Text>;
  return <Typography.Text>{tools.length} 个工具</Typography.Text>;
}

function ToolsToggle({ capId }: { capId: string }) {
  const queryClient = useQueryClient();
  const { data: tools = [] } = useQuery({
    queryKey: ["cap-tools", capId],
    queryFn: () => capabilitiesApi.tools(capId),
  });
  const toggleMutation = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      capabilitiesApi.setTool(capId, name, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["cap-tools", capId] }),
  });
  if (tools.length === 0) return <Typography.Text type="secondary">无工具</Typography.Text>;
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      {tools.map((t: CapabilityToolOut) => (
        <div key={t.tool_name} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <Space size={4}>
            <Typography.Text style={{ fontSize: 13 }}>{t.tool_name}</Typography.Text>
            {t.description && (
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                {t.description.slice(0, 60)}
              </Typography.Text>
            )}
          </Space>
          <Switch
            size="small"
            checked={t.enabled}
            loading={toggleMutation.isPending}
            onChange={(checked) => toggleMutation.mutate({ name: t.tool_name, enabled: checked })}
          />
        </div>
      ))}
    </Space>
  );
}

export default function McpServicesPage() {
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [smokeResult, setSmokeResult] = useState<CapabilityCreatedOut | null>(null);
  const [form] = Form.useForm();
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");

  const { data: allCaps = [] } = useQuery({
    queryKey: ["capabilities", "all"],
    queryFn: () => capabilitiesApi.list(true),
  });
  const mcpCaps = allCaps.filter((c) => c.type === "mcp");

  const createMutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const env: Record<string, string> = {};
      const secretEnv: Record<string, string> = {};
      const envPairs = (values.env_pairs as Array<{ key: string; value: string }>) || [];
      const secretPairs = (values.secret_pairs as Array<{ key: string; value: string }>) || [];
      envPairs.forEach((p) => { if (p.key) env[p.key] = p.value; });
      secretPairs.forEach((p) => { if (p.key) secretEnv[p.key] = p.value; });

      const payload: Record<string, unknown> = { transport: values.transport };
      if (values.transport === "stdio") {
        payload.command = values.command;
        payload.args = (values.args as string || "").split(/\s+/).filter(Boolean);
        if (Object.keys(env).length) payload.env = env;
      } else {
        payload.url = values.url;
      }

      return capabilitiesApi.create({
        type: "mcp",
        category: "external",
        name: values.name as string,
        description: values.description as string,
        version: (values.version as string) || "0.1.0",
        risk_level: values.risk_level as string,
        payload,
        secret_env: Object.keys(secretEnv).length ? secretEnv : undefined,
      });
    },
    onSuccess: (result) => {
      setSmokeResult(result);
      queryClient.invalidateQueries({ queryKey: ["capabilities"] });
      form.resetFields();
      setTransport("stdio");
    },
    onError: () => message.error("注册失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => capabilitiesApi.del(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["capabilities"] });
      message.success("已删除");
    },
  });

  const toggleEnabled = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      capabilitiesApi.update(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["capabilities"] }),
  });

  const handleSubmit = async () => {
    const values = await form.validateFields();
    createMutation.mutate(values);
  };

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between" }}>
        <Typography.Title level={5} style={{ margin: 0 }}>
          <ApiOutlined /> MCP 服务
        </Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => { setSmokeResult(null); setModalOpen(true); }}>
          注册 MCP Server
        </Button>
      </div>

      <Table<CapabilityOut>
        rowKey="id"
        dataSource={mcpCaps}
        size="small"
        pagination={false}
        expandable={{
          expandedRowRender: (record) => <ToolsToggle capId={record.id} />,
        }}
        columns={[
          {
            title: "名称",
            dataIndex: "name",
            render: (name: string, r) => (
              <Space direction="vertical" size={0}>
                <Typography.Text strong>{name}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  {r.description}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: "传输",
            dataIndex: "payload",
            width: 80,
            render: (payload: Record<string, unknown>) => (
              <Tag>{(payload.transport as string) || "—"}</Tag>
            ),
          },
          {
            title: "工具",
            width: 80,
            render: (_, r) => <ToolsCell capId={r.id} />,
          },
          {
            title: "风险",
            dataIndex: "risk_level",
            width: 70,
            render: (risk: string) => <Tag color={RISK_COLORS[risk]}>{risk}</Tag>,
          },
          {
            title: "健康",
            dataIndex: "health_status",
            width: 80,
            render: (status: string) => {
              const meta = HEALTH_META[status] || HEALTH_META.unknown;
              return <Tag color={meta.color}>{meta.label}</Tag>;
            },
          },
          {
            title: "启用",
            dataIndex: "enabled",
            width: 60,
            render: (enabled: boolean, r) => (
              <Switch
                size="small"
                checked={enabled}
                onChange={(checked) => toggleEnabled.mutate({ id: r.id, enabled: checked })}
              />
            ),
          },
          {
            title: "",
            width: 50,
            render: (_, r) => (
              <Popconfirm title="删除此能力？" onConfirm={() => deleteMutation.mutate(r.id)}>
                <Button size="small" type="text" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            ),
          },
        ]}
      />

      <Modal
        title="注册 MCP Server"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleSubmit}
        confirmLoading={createMutation.isPending}
        width={560}
        destroyOnClose
      >
        {smokeResult ? (
          <Alert
            type={smokeResult.smoke.passed ? "success" : "error"}
            message={smokeResult.smoke.passed ? "冒烟测试通过" : "冒烟测试失败"}
            description={
              <div>
                <p style={{ margin: "4px 0" }}>
                  能力 <strong>{smokeResult.capability.name}</strong> 已注册
                  {!smokeResult.smoke.passed && "（已标记为禁用，修复后可重新启用）"}
                </p>
                {smokeResult.smoke.checks.map((r, i) => (
                  <div key={i} style={{ fontSize: 12 }}>
                    {r.passed ? (
                      <CheckCircleOutlined style={{ color: "green" }} />
                    ) : (
                      <CloseCircleOutlined style={{ color: "red" }} />
                    )}{" "}
                    {r.tool}: {r.error || "OK"}
                  </div>
                ))}
              </div>
            }
            action={
              <Button size="small" onClick={() => { setSmokeResult(null); setModalOpen(false); }}>
                关闭
              </Button>
            }
          />
        ) : (
          <Form form={form} layout="vertical" initialValues={{ transport: "stdio", version: "0.1.0", risk_level: "read" }}>
            <Form.Item name="name" label="名称" rules={[{ required: true, message: "必填" }, { pattern: /^[a-zA-Z0-9_.-]+$/, message: "仅字母数字 . - _" }]}>
              <Input placeholder="如: filesystem-server" />
            </Form.Item>
            <Form.Item name="description" label="描述" rules={[{ required: true }]}>
              <Input.TextArea rows={2} placeholder="一句话描述此 MCP Server 提供的能力" />
            </Form.Item>
            <Space style={{ width: "100%" }} size="small">
              <Form.Item name="version" label="版本" style={{ width: 120 }}>
                <Input />
              </Form.Item>
              <Form.Item name="risk_level" label="风险级别" style={{ width: 150 }}>
                <Select options={[{ value: "read" }, { value: "write" }, { value: "dangerous" }]} />
              </Form.Item>
            </Space>
            <Form.Item name="transport" label="传输方式">
              <Radio.Group onChange={(e) => setTransport(e.target.value)}>
                <Radio.Button value="stdio">stdio</Radio.Button>
                <Radio.Button value="http">http</Radio.Button>
              </Radio.Group>
            </Form.Item>
            {transport === "stdio" ? (
              <>
                <Form.Item name="command" label="启动命令" rules={[{ required: true }]}>
                  <Input placeholder="如: npx -y @modelcontextprotocol/server-filesystem" />
                </Form.Item>
                <Form.Item name="args" label="参数（空格分隔）">
                  <Input placeholder="如: /tmp/data /tmp/logs" />
                </Form.Item>
              </>
            ) : (
              <Form.Item name="url" label="URL" rules={[{ required: true }]}>
                <Input placeholder="https://..." />
              </Form.Item>
            )}
            <Form.Item name="env_pairs" label="环境变量">
              <Form.List name="env_pairs">
                {(fields, { add, remove }) => (
                  <>
                    {fields.map(({ key, name }) => (
                      <Space key={key} style={{ display: "flex", marginBottom: 4 }} align="baseline">
                        <Form.Item name={[name, "key"]} noStyle><Input placeholder="KEY" style={{ width: 140 }} /></Form.Item>
                        <Form.Item name={[name, "value"]} noStyle><Input placeholder="VALUE" style={{ width: 200 }} /></Form.Item>
                        <a onClick={() => remove(name)}>删除</a>
                      </Space>
                    ))}
                    <Button size="small" type="dashed" onClick={() => add({})} icon={<PlusOutlined />}>添加</Button>
                  </>
                )}
              </Form.List>
            </Form.Item>
            <Form.Item name="secret_pairs" label="敏感环境变量（加密存储）">
              <Form.List name="secret_pairs">
                {(fields, { add, remove }) => (
                  <>
                    {fields.map(({ key, name }) => (
                      <Space key={key} style={{ display: "flex", marginBottom: 4 }} align="baseline">
                        <Form.Item name={[name, "key"]} noStyle><Input placeholder="KEY" style={{ width: 140 }} /></Form.Item>
                        <Form.Item name={[name, "value"]} noStyle><Input.Password placeholder="SECRET" style={{ width: 200 }} /></Form.Item>
                        <a onClick={() => remove(name)}>删除</a>
                      </Space>
                    ))}
                    <Button size="small" type="dashed" onClick={() => add({})} icon={<PlusOutlined />}>添加</Button>
                  </>
                )}
              </Form.List>
            </Form.Item>
          </Form>
        )}
      </Modal>
    </div>
  );
}
