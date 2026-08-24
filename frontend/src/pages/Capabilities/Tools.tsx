/**
 * 工具页（前端设计 §3.4）：type=tool 的能力管理。
 * 内置工具只读；外部工具可注册（OpenAI 函数签名 payload.schema）。
 */
import { useState } from "react";
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Tag,
  Switch,
  Space,
  Typography,
  message,
  Popconfirm,
} from "antd";
import { PlusOutlined, ToolOutlined, DeleteOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { capabilitiesApi } from "@/api/capabilities";
import type { CapabilityOut } from "@/api/types";

const RISK_COLORS: Record<string, string> = {
  read: "green",
  write: "orange",
  dangerous: "red",
};

export default function ToolsPage() {
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  const { data: allCaps = [] } = useQuery({
    queryKey: ["capabilities", "all"],
    queryFn: () => capabilitiesApi.list(true),
  });
  const toolCaps = allCaps.filter((c) => c.type === "tool");

  const createMutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      let schema: unknown = {};
      if (values.schema_text) {
        try {
          schema = JSON.parse(values.schema_text as string);
        } catch {
          throw new Error("schema JSON 解析失败");
        }
      }
      return capabilitiesApi.create({
        type: "tool",
        category: "external",
        name: values.name as string,
        description: values.description as string,
        version: (values.version as string) || "0.1.0",
        risk_level: values.risk_level as string,
        payload: { schema },
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["capabilities"] });
      message.success("工具已注册");
      setModalOpen(false);
      form.resetFields();
    },
    onError: (e: Error) => message.error(e.message || "注册失败"),
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
          <ToolOutlined /> 工具
        </Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
          注册工具
        </Button>
      </div>

      <Table<CapabilityOut>
        rowKey="id"
        dataSource={toolCaps}
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
            title: "类别",
            dataIndex: "category",
            width: 80,
            render: (cat: string) => (
              <Tag>{cat === "builtin" ? "内置" : cat === "internal" ? "内部" : "外部"}</Tag>
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
            title: "启用",
            dataIndex: "enabled",
            width: 60,
            render: (enabled: boolean, r) => (
              <Switch
                size="small"
                checked={enabled}
                disabled={r.category === "builtin"}
                onChange={(checked) => toggleEnabled.mutate({ id: r.id, enabled: checked })}
              />
            ),
          },
          {
            title: "",
            width: 50,
            render: (_, r) =>
              r.category !== "builtin" ? (
                <Popconfirm title="删除此工具？" onConfirm={() => deleteMutation.mutate(r.id)}>
                  <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              ) : null,
          },
        ]}
      />

      <Modal
        title="注册工具"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.validateFields().then((v) => createMutation.mutate(v))}
        confirmLoading={createMutation.isPending}
        width={520}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={{ version: "0.1.0", risk_level: "read" }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }, { pattern: /^[a-zA-Z0-9_.-]+$/, message: "仅字母数字 . - _" }]}>
            <Input placeholder="如: web_search" />
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
          <Form.Item name="schema_text" label="函数签名（JSON Schema，可选）">
            <Input.TextArea
              rows={6}
              style={{ fontFamily: "monospace", fontSize: 12 }}
              placeholder='{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}'
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
