/**
 * 插件页（前端设计 §3.4）：type=plugin 的能力管理。
 * 注册方式与 MCP 类似（stdio/http transport），但定位是扩展插件。
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
  Popconfirm,
} from "antd";
import { PlusOutlined, AppstoreAddOutlined, DeleteOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { capabilitiesApi } from "@/api/capabilities";
import type { CapabilityOut } from "@/api/types";

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

export default function PluginsPage() {
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");

  const { data: allCaps = [] } = useQuery({
    queryKey: ["capabilities", "all"],
    queryFn: () => capabilitiesApi.list(true),
  });
  const pluginCaps = allCaps.filter((c) => c.type === "plugin");

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
        type: "plugin",
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
      queryClient.invalidateQueries({ queryKey: ["capabilities"] });
      message.success(
        result.smoke.passed
          ? "插件已注册，冒烟测试通过"
          : "插件已注册但冒烟测试失败（已禁用）",
      );
      setModalOpen(false);
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

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between" }}>
        <Typography.Title level={5} style={{ margin: 0 }}>
          <AppstoreAddOutlined /> 插件
        </Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
          注册插件
        </Button>
      </div>

      <Table<CapabilityOut>
        rowKey="id"
        dataSource={pluginCaps}
        size="small"
        pagination={false}
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
            title: "版本",
            dataIndex: "version",
            width: 70,
            render: (v: string) => <Typography.Text style={{ fontSize: 12 }}>{v}</Typography.Text>,
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
              <Popconfirm title="删除此插件？" onConfirm={() => deleteMutation.mutate(r.id)}>
                <Button size="small" type="text" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            ),
          },
        ]}
      />

      <Modal
        title="注册插件"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.validateFields().then((v) => createMutation.mutate(v))}
        confirmLoading={createMutation.isPending}
        width={560}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={{ transport: "stdio", version: "0.1.0", risk_level: "read" }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }, { pattern: /^[a-zA-Z0-9_.-]+$/, message: "仅字母数字 . - _" }]}>
            <Input placeholder="如: code-interpreter" />
          </Form.Item>
          <Form.Item name="description" label="描述" rules={[{ required: true }]}>
            <Input.TextArea rows={2} />
          </Form.Item>
          <Space size="small">
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
                <Input placeholder="如: npx -y some-plugin" />
              </Form.Item>
              <Form.Item name="args" label="参数（空格分隔）">
                <Input />
              </Form.Item>
            </>
          ) : (
            <Form.Item name="url" label="URL" rules={[{ required: true }]}>
              <Input placeholder="https://..." />
            </Form.Item>
          )}
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
      </Modal>
    </div>
  );
}
