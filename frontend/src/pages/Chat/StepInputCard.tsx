/**
 * 子任务补输入卡（P1-4 收尾）：执行到某个子任务才发现缺必需材料时就地补齐，
 * 而不是让整个 run 失败重来（run 级门在调用模型前硬失败，这里已是执行中的中间态）。
 *
 * 载荷真源：backend/app/modules/workers/preflight.py::StepInputResolution.payload
 * `{step_id, step_name, sub_ref, missing:[字段名], message, inputs:[{name,type,required,
 *   description,example,label,provided,value}]}`，由 `input_gate` 节点的 interrupt 经
 * runtime `_pause` 落成 `confirmation_request` 事件（`reason="input_required"`）。
 *
 * 提交走 `POST /runs/{id}/confirm`，**只带 inputs、answer 留空**：门恢复后不消费
 * interrupt 返回值（唯一事实源是库里的取值），故空 answer 也是合法恢复。
 */
import { useState } from "react";
import { Button, Form, Space, Tag, Typography } from "antd";
import { CheckOutlined, ExclamationCircleOutlined } from "@ant-design/icons";
import type { RunInputSpec } from "@/api/types";
import type { CardContext } from "./CardRenderer";
import { Shell } from "./CardShell";
import type { CardComponent } from "./CardShell";
import { fieldControl } from "./MissingInputsCard";

const { Text } = Typography;

interface StepInputSpec extends RunInputSpec {
  /** 人话描述：`quote_file（file，必填）` */
  label?: string;
}

export interface StepInputPayload {
  step_id: string;
  step_name: string;
  sub_ref: string;
  /** 缺失的字段名（必填且未提供） */
  missing: string[];
  /** 给用户看的说明（含逐项材料要求） */
  message: string;
  /** 全部声明明细（含已提供的值） */
  inputs: StepInputSpec[];
}

/** 解析 interrupt 载荷；不像补输入载荷（无声明明细）返回 null，由调用方兜底展示。 */
export function parseStepInput(raw: unknown): StepInputPayload | null {
  const p = (raw ?? {}) as Record<string, unknown>;
  const specs: StepInputSpec[] = [];
  if (Array.isArray(p.inputs)) {
    for (const entry of p.inputs) {
      const item = (entry ?? {}) as Partial<StepInputSpec>;
      if (typeof item.name !== "string" || !item.name) continue;
      specs.push({
        name: item.name,
        type: item.type ?? "text",
        required: Boolean(item.required),
        description: typeof item.description === "string" ? item.description : "",
        example: typeof item.example === "string" ? item.example : "",
        provided: Boolean(item.provided),
        value: typeof item.value === "string" ? item.value : null,
        label: typeof item.label === "string" ? item.label : undefined,
      });
    }
  }
  if (specs.length === 0) return null;
  return {
    step_id: typeof p.step_id === "string" ? p.step_id : "",
    step_name: typeof p.step_name === "string" ? p.step_name : "",
    sub_ref: typeof p.sub_ref === "string" ? p.sub_ref : "",
    missing: Array.isArray(p.missing) ? p.missing.map(String) : [],
    message: typeof p.message === "string" ? p.message : "",
    inputs: specs,
  };
}

const StepInputCardForm = ({ ctx, parsed }: { ctx: CardContext; parsed: StepInputPayload }) => {
  const [submitting, setSubmitting] = useState(false);
  // 只需补「必填且未提供」的项；已提供的值已按会话累积在库里
  const fields = parsed.inputs.filter((s) => s.required && !s.provided);
  const provided = parsed.inputs.filter((s) => s.provided);

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
      // answer 留空：这道门只认补的取值，空答复也是合法恢复
      await Promise.resolve(ctx.onConfirm("", inputs));
    } finally {
      // 失败提示由调用方负责（提交链路已弹错误），这里只保证按钮可重试
      setSubmitting(false);
    }
  };

  return (
    <>
      {provided.length > 0 && (
        <Text type="secondary" style={{ display: "block", marginBottom: 8, fontSize: 12 }}>
          已提供：
          {provided.map((s) => `${s.name}=${s.value ?? ""}`).join("，")}
          （会话内已提供的输入无需重填）
        </Text>
      )}
      {fields.length > 0 ? (
        <Form layout="vertical" size="small" onFinish={handleFinish}>
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
            <CheckOutlined /> 补齐并继续
          </Button>
          <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
            补齐后从暂停点继续，已产出的进度不重跑；file 类型也可在输入栏添加附件顶替
          </Text>
        </Form>
      ) : (
        <Text type="secondary">本次缺的输入已全部补齐，可直接继续执行。</Text>
      )}
    </>
  );
};

export const StepInputCard: CardComponent = ({ ctx }) => {
  const parsed = parseStepInput(ctx.inner);
  const fallback = String(ctx.inner.message ?? "执行到子任务时发现缺少必需输入");
  if (parsed === null) {
    // 载荷不含声明明细（不应出现）时只报事实，不假装能补
    return (
      <Shell
        severity="warn"
        title="子任务缺少必需输入"
        icon={<ExclamationCircleOutlined style={{ color: "var(--ant-color-warning)" }} />}
      >
        <Text style={{ whiteSpace: "pre-wrap" }}>{fallback}</Text>
      </Shell>
    );
  }
  return (
    <Shell
      severity="warn"
      title={`子任务「${parsed.step_name}」缺少必需输入`}
      icon={<ExclamationCircleOutlined style={{ color: "var(--ant-color-warning)" }} />}
    >
      <Text style={{ display: "block", marginBottom: 8, whiteSpace: "pre-wrap" }}>
        {parsed.message || fallback}
      </Text>
      <StepInputCardForm ctx={ctx} parsed={parsed} />
    </Shell>
  );
};

export default StepInputCard;
