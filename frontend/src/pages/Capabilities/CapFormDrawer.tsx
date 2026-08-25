/**
 * 能力表单 Drawer（前端设计 §4 右侧 Drawer 表单规范）。
 * tool/mcp/plugin/skill 四类型共用：注册（POST，附冒烟报告视图）与编辑（PATCH）。
 * 编辑时 name 只读；mcp/plugin 已保存密钥不回显（留空 = 保持旧值）。
 */
import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Form,
  Input,
  Radio,
  Select,
  Space,
  message,
} from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { capabilitiesApi, type CapabilityCreatedOut } from "@/api/capabilities";
import type { CapabilityOut } from "@/api/types";

export type CapType = "tool" | "skill" | "mcp" | "plugin";

const TYPE_LABEL: Record<CapType, string> = {
  tool: "工具",
  skill: "技能",
  mcp: "MCP Server",
  plugin: "插件",
};

// 标题（中英文间距正确）
const DRAWER_TITLE: Record<CapType, [string, string]> = {
  tool: ["注册工具", "编辑工具"],
  skill: ["注册技能", "编辑技能"],
  mcp: ["注册 MCP Server", "编辑 MCP Server"],
  plugin: ["注册插件", "编辑插件"],
};

const NAME_PLACEHOLDER: Record<CapType, string> = {
  tool: "如: web_search",
  skill: "如: legal-doc-writer",
  mcp: "如: filesystem-server",
  plugin: "如: code-interpreter",
};

/** SKILL.md 正文 = 剥离 --- 包围 yaml 头之后的剩余部分。 */
function parseSkillBody(md: string): string {
  const text = md.trim();
  if (!text.startsWith("---")) return text;
  const parts = text.split("---");
  if (parts.length < 3) return text;
  return parts.slice(2).join("---").trim();
}

/** name/description 合成 yaml 头（JSON.stringify 转义，防冒号/换行破坏 yaml 结构）。 */
function composeSkillMd(name: string, description: string, body: string): string {
  return `---\nname: ${JSON.stringify(name)}\ndescription: ${JSON.stringify(description)}\n---\n\n${body.trim()}\n`;
}

function buildPayload(type: CapType, values: Record<string, unknown>): Record<string, unknown> {
  if (type === "tool") {
    let schema: unknown = {};
    try {
      schema = values.schema_text ? JSON.parse(values.schema_text as string) : {};
    } catch {
      throw new Error("函数签名 JSON 解析失败");
    }
    return { schema };
  }
  if (type === "skill") {
    return {
      skill_md: composeSkillMd(
        String(values.name),
        String(values.description),
        String(values.skill_body ?? ""),
      ),
    };
  }
  // mcp / plugin
  const env: Record<string, string> = {};
  const envPairs = (values.env_pairs as Array<{ key: string; value: string }>) || [];
  envPairs.forEach((p) => {
    if (p.key) env[p.key] = p.value;
  });
  const payload: Record<string, unknown> = { transport: values.transport };
  if (values.transport === "stdio") {
    payload.command = values.command;
    payload.args = ((values.args as string) || "").split(/\s+/).filter(Boolean);
    if (Object.keys(env).length) payload.env = env;
  } else {
    payload.url = values.url;
  }
  return payload;
}

export default function CapFormDrawer({
  type,
  cap,
  open,
  onClose,
}: {
  type: CapType;
  cap: CapabilityOut | null;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [form] = Form.useForm();
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");
  const [smokeResult, setSmokeResult] = useState<CapabilityCreatedOut | null>(null);
  const isEdit = cap !== null;

  // 开抽屉时预填（编辑）或重置（注册）
  useEffect(() => {
    if (!open) return;
    setSmokeResult(null);
    if (cap) {
      const payload = cap.payload || {};
      form.setFieldsValue({
        name: cap.name,
        description: cap.description,
        version: cap.version,
        risk_level: cap.risk_level,
        transport: payload.transport,
        command: payload.command as string | undefined,
        args: Array.isArray(payload.args) ? (payload.args as string[]).join(" ") : undefined,
        url: payload.url as string | undefined,
        env_pairs: Object.entries(payload.env || {}).map(([key, value]) => ({
          key,
          value: String(value),
        })),
        secret_pairs: [],
        schema_text:
          type === "tool" && payload.schema
            ? JSON.stringify(payload.schema, null, 2)
            : undefined,
        skill_body: type === "skill" ? parseSkillBody(String(payload.skill_md || "")) : undefined,
      });
      setTransport((payload.transport as "stdio" | "http") || "stdio");
    } else {
      form.resetFields();
      setTransport("stdio");
    }
  }, [open, cap, form, type]);

  const mutation = useMutation({
    mutationFn: async (values: Record<string, unknown>) => {
      const payload = buildPayload(type, values);
      const secretPairs = (values.secret_pairs as Array<{ key: string; value: string }>) || [];
      const secretEnv: Record<string, string> = {};
      secretPairs.forEach((p) => {
        if (p.key) secretEnv[p.key] = p.value;
      });

      if (cap) {
        const body: Record<string, unknown> = {
          description: values.description,
          version: values.version,
          risk_level: values.risk_level,
          payload,
        };
        // 未输入新密钥则不提交 secret_env（后端保留旧密钥）
        if (Object.keys(secretEnv).length) body.secret_env = secretEnv;
        return capabilitiesApi.update(cap.id, body);
      }
      return capabilitiesApi.create({
        type,
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
      if (cap) {
        message.success("已保存");
        onClose();
      } else {
        // 注册成功：切换冒烟报告视图（失败的能力也已落库，修复后可编辑再启用）
        setSmokeResult(result as CapabilityCreatedOut);
      }
    },
    onError: (e: Error) => message.error(e.message || "提交失败"),
  });

  const handleSubmit = async () => {
    const values = await form.validateFields();
    mutation.mutate(values);
  };

  const secretLabel =
    cap?.has_secret_env && (type === "mcp" || type === "plugin")
      ? "敏感环境变量（加密存储；已保存的密钥不回显，留空 = 保持不变）"
      : "敏感环境变量（加密存储）";

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={560}
      destroyOnClose
      title={cap ? DRAWER_TITLE[type][1] : DRAWER_TITLE[type][0]}
      footer={
        smokeResult ? (
          <Space style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button onClick={onClose}>关闭</Button>
          </Space>
        ) : (
          <Space style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button onClick={onClose}>取消</Button>
            <Button type="primary" loading={mutation.isPending} onClick={handleSubmit}>
              {isEdit ? "保存" : "注册"}
            </Button>
          </Space>
        )
      }
    >
      {smokeResult ? (
        <Alert
          type={smokeResult.smoke.passed ? "success" : "error"}
          message={
            smokeResult.smoke.passed
              ? `冒烟测试通过，${TYPE_LABEL[type]}已启用`
              : "冒烟测试失败（能力已保存但禁用，可编辑修复后启用）"
          }
          description={
            <div>
              <div style={{ margin: "4px 0", fontSize: 12 }}>{smokeResult.smoke.summary}</div>
              {smokeResult.smoke.checks.map((r, i) => (
                <div key={i} style={{ fontSize: 12 }}>
                  {r.ok ? (
                    <CheckCircleOutlined style={{ color: "green" }} />
                  ) : (
                    <CloseCircleOutlined style={{ color: "red" }} />
                  )}{" "}
                  {r.name}: {r.detail}
                </div>
              ))}
            </div>
          }
        />
      ) : (
        <Form
          form={form}
          layout="vertical"
          initialValues={{ version: "0.1.0", risk_level: "read", transport: "stdio" }}
        >
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true },
              { pattern: /^[a-zA-Z0-9_.-]+$/, message: "仅字母数字 . - _" },
            ]}
          >
            <Input placeholder={NAME_PLACEHOLDER[type]} disabled={isEdit} />
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

          {type === "tool" && (
            <Form.Item
              name="schema_text"
              label="函数签名（JSON Schema）"
              rules={[{ required: true, message: "外部工具必须提供函数签名" }]}
            >
              <Input.TextArea
                rows={8}
                style={{ fontFamily: "monospace", fontSize: 12 }}
                placeholder='{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}'
              />
            </Form.Item>
          )}

          {type === "skill" && (
            <Form.Item
              name="skill_body"
              label="技能正文（Markdown，注册时与名称/描述合成 SKILL.md）"
              rules={[{ required: true, message: "技能正文不能为空" }]}
            >
              <Input.TextArea
                rows={12}
                style={{ fontFamily: "monospace", fontSize: 12 }}
                placeholder={"## 使用场景\n\n…"}
              />
            </Form.Item>
          )}

          {(type === "mcp" || type === "plugin") && (
            <>
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
              <Form.Item label="环境变量">
                <Form.List name="env_pairs">
                  {(fields, { add, remove }) => (
                    <>
                      {fields.map(({ key, name }) => (
                        <Space key={key} style={{ display: "flex", marginBottom: 4 }} align="baseline">
                          <Form.Item name={[name, "key"]} noStyle>
                            <Input placeholder="KEY" style={{ width: 140 }} />
                          </Form.Item>
                          <Form.Item name={[name, "value"]} noStyle>
                            <Input placeholder="VALUE" style={{ width: 220 }} />
                          </Form.Item>
                          <a onClick={() => remove(name)}>删除</a>
                        </Space>
                      ))}
                      <Button size="small" type="dashed" onClick={() => add({})} icon={<PlusOutlined />}>
                        添加
                      </Button>
                    </>
                  )}
                </Form.List>
              </Form.Item>
              <Form.Item label={secretLabel}>
                <Form.List name="secret_pairs">
                  {(fields, { add, remove }) => (
                    <>
                      {fields.map(({ key, name }) => (
                        <Space key={key} style={{ display: "flex", marginBottom: 4 }} align="baseline">
                          <Form.Item name={[name, "key"]} noStyle>
                            <Input placeholder="KEY" style={{ width: 140 }} />
                          </Form.Item>
                          <Form.Item name={[name, "value"]} noStyle>
                            <Input.Password placeholder="SECRET" style={{ width: 220 }} />
                          </Form.Item>
                          <a onClick={() => remove(name)}>删除</a>
                        </Space>
                      ))}
                      <Button size="small" type="dashed" onClick={() => add({})} icon={<PlusOutlined />}>
                        添加
                      </Button>
                    </>
                  )}
                </Form.List>
              </Form.Item>
            </>
          )}
        </Form>
      )}
    </Drawer>
  );
}
