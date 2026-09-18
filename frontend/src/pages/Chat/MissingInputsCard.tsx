/**
 * 补输入表单（P1-4）：run 因输入契约预检失败时，就地把缺的必填项补齐并重发。
 *
 * 载荷真源：backend/app/modules/engine/runtime.py::_finalize_input_contract
 * `{code:"missing_inputs", detail, missing:[字段名], inputs:[{name,type,required,description,example,provided,value}]}`
 * （同一份载荷既是 error 事件 payload，也是 run.error）。后端 docstring 明确
 * 「补齐后可原样重发」：同会话 inputs 快照按时间累加，故只需补当前缺的项；
 * 重发即重新发一条同样的用户消息，没有单独的 retry 接口。
 */
import { useState } from "react";
import { Alert, Button, Form, Input, InputNumber, Space, Tag, Typography } from "antd";
import type { RunInputSpec } from "@/api/types";

const { Text, Paragraph } = Typography;

export interface MissingInputsPayload {
  detail: string;
  /** 缺失的字段名（必填未提供） */
  missing: string[];
  /** 全部声明明细（含已提供的值） */
  specs: RunInputSpec[];
}

/** 解析 error 事件 payload / run.error；非 missing_inputs 返回 null。 */
export function parseMissingInputs(raw: unknown): MissingInputsPayload | null {
  const p = (raw ?? {}) as {
    code?: unknown;
    detail?: unknown;
    missing?: unknown;
    inputs?: unknown;
  };
  if (p.code !== "missing_inputs") return null;
  const missing = Array.isArray(p.missing) ? p.missing.map(String) : [];
  const specs: RunInputSpec[] = [];
  if (Array.isArray(p.inputs)) {
    for (const entry of p.inputs) {
      const item = (entry ?? {}) as Partial<RunInputSpec>;
      if (typeof item.name !== "string" || !item.name) continue;
      specs.push({
        name: item.name,
        type: item.type ?? "text",
        required: Boolean(item.required),
        description: typeof item.description === "string" ? item.description : "",
        example: typeof item.example === "string" ? item.example : "",
        provided: Boolean(item.provided),
        value: typeof item.value === "string" ? item.value : null,
      });
    }
  }
  // 兼容只带字段名的载荷（如从 result.extra.missing 兜底读到）：退化成纯文本项
  for (const name of missing) {
    if (specs.some((s) => s.name === name)) continue;
    specs.push({
      name,
      type: "text",
      required: true,
      description: "",
      example: "",
      provided: false,
      value: null,
    });
  }
  if (specs.length === 0) return null;
  return {
    detail: typeof p.detail === "string" ? p.detail : "",
    missing,
    specs,
  };
}

export function fieldControl(spec: RunInputSpec) {
  const placeholder = spec.example || spec.description || `请输入 ${spec.name}`;
  if (spec.type === "number") {
    return <InputNumber style={{ width: "100%" }} placeholder={placeholder} />;
  }
  if (spec.type === "json") {
    return <Input.TextArea rows={3} placeholder={spec.example || `{"key": "value"}`} />;
  }
  if (spec.type === "date") {
    return <Input placeholder={spec.example || "YYYY-MM-DD"} />;
  }
  if (spec.type === "file") {
    // file 类型同样接受文本值（后端只判「有无取值」）；也可在输入栏加附件顶替
    return <Input placeholder={spec.example || "附件文件名（或在输入栏添加附件顶替）"} />;
  }
  return <Input placeholder={placeholder} />;
}

export default function MissingInputsCard({
  payload,
  text,
  onSubmit,
}: {
  payload: MissingInputsPayload;
  /** 原 run 的用户消息文本：重发时原样带回 */
  text: string;
  onSubmit: (
    text: string,
    inputs: Record<string, string | number | boolean | null>,
  ) => Promise<void>;
}) {
  const [form] = Form.useForm();
  const [submitting, setSubmitting] = useState(false);
  // 只需用户补「必填且未提供」的项；已提供的靠同会话 inputs 快照累加
  const fields = payload.specs.filter((s) => s.required && !s.provided);
  const provided = payload.specs.filter((s) => s.provided);
  const canResend = text.trim().length > 0 && fields.length > 0;

  const handleFinish = async (values: Record<string, unknown>) => {
    const inputs: Record<string, string> = {};
    for (const spec of fields) {
      const value = values[spec.name];
      if (value === undefined || value === null) continue;
      const trimmed = String(value).trim();
      if (trimmed) inputs[spec.name] = trimmed;
    }
    setSubmitting(true);
    try {
      await onSubmit(text, inputs);
    } finally {
      // 失败提示由调用方负责（重发走会话发送链路，错误已在输入侧弹出）
      setSubmitting(false);
    }
  };

  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 12 }}
      message="缺少必需输入，已在调用模型前拦截（未消耗模型调用）"
      description={
        <div>
          {payload.detail && (
            <Paragraph style={{ marginBottom: 8, whiteSpace: "pre-wrap" }}>
              {payload.detail}
            </Paragraph>
          )}
          {provided.length > 0 && (
            <Paragraph type="secondary" style={{ marginBottom: 8, fontSize: 12 }}>
              已提供：
              {provided.map((s) => `${s.name}=${s.value ?? ""}`).join("，")}
              （同会话已提供的输入无需重填）
            </Paragraph>
          )}
          {canResend ? (
            <Form form={form} layout="vertical" size="small" onFinish={handleFinish}>
              {fields.map((spec) => (
                <Form.Item
                  key={spec.name}
                  name={spec.name}
                  label={
                    <Space size={4}>
                      <Text>{spec.name}</Text>
                      <Tag>{spec.type}</Tag>
                    </Space>
                  }
                  tooltip={spec.description || undefined}
                  rules={[{ required: true, message: `请填写 ${spec.name}` }]}
                  style={{ marginBottom: 12 }}
                >
                  {fieldControl(spec)}
                </Form.Item>
              ))}
              <Button type="primary" size="small" htmlType="submit" loading={submitting}>
                补齐并重发
              </Button>
              <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                重发会按同一会话重新发起执行；file 类型也可在输入栏添加附件后重发
              </Text>
            </Form>
          ) : (
            <Text type="secondary">
              原输入文本已缺失，无法原样重发：请在下方输入栏补充说明后重新发送
            </Text>
          )}
        </div>
      }
    />
  );
}
