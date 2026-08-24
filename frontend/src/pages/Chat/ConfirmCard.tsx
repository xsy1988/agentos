/**
 * 确认卡片（前端设计 §3.1 确认卡片非模态）。
 * 出现在输入框上方而非全屏遮罩；dangerous 工具红色强调。
 *
 * 后端 confirmation_request 事件 payload 结构（runtime._pause_for_confirmation）：
 * - 计划确认：{reason: "plan_review", payload: {plan: [{seq, text, status}]}}
 * - 高危工具：{reason: "high_risk_tool", payload: {calls: [{name, args}], risk_levels: {name: level}}}
 */
import { Card, Button, Typography, Tag, Space } from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  ExclamationCircleOutlined,
} from "@ant-design/icons";

interface PlanItem {
  seq: number;
  text: string;
  status: string;
}

interface RiskyCall {
  name: string;
  args: Record<string, unknown>;
}

export default function ConfirmCard({
  payload,
  onConfirm,
  onReject,
}: {
  payload: Record<string, unknown>;
  onConfirm: () => void;
  onReject: () => void;
}) {
  const reason = payload.reason as string;
  const inner = (payload.payload ?? {}) as Record<string, unknown>;
  const isHighRisk = reason === "high_risk_tool";
  const hasDangerous = Object.values(
    (inner.risk_levels ?? {}) as Record<string, string>,
  ).includes("dangerous");

  const plan = (inner.plan ?? []) as PlanItem[];
  const calls = (inner.calls ?? []) as RiskyCall[];

  const title =
    reason === "plan_review"
      ? "Agent 生成了执行计划，请确认"
      : reason === "high_risk_tool"
        ? "高危操作，提交前请确认"
        : "需要确认";

  return (
    <Card
      size="small"
      style={{
        margin: "0 16px 8px",
        borderColor: isHighRisk ? "var(--ant-color-error)" : "var(--ant-color-warning)",
        borderWidth: isHighRisk ? 2 : 1,
        background: isHighRisk
          ? "rgba(255,77,79,0.06)"
          : "rgba(250,173,20,0.06)",
      }}
    >
      <Space size={6} style={{ marginBottom: 8 }}>
        {isHighRisk ? (
          <ExclamationCircleOutlined style={{ color: "var(--ant-color-error)" }} />
        ) : null}
        <Typography.Text strong>{title}</Typography.Text>
      </Space>

      {reason === "plan_review" && plan.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          {plan.map((step, i) => (
            <div key={step.seq ?? i} style={{ fontSize: 13, marginBottom: 2 }}>
              {step.seq ?? i + 1}. {step.text}
            </div>
          ))}
        </div>
      )}

      {reason === "high_risk_tool" && calls.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          {calls.map((c, i) => {
            const level = ((inner.risk_levels ?? {}) as Record<string, string>)[c.name];
            return (
              <div key={i} style={{ marginBottom: 6 }}>
                <Space size={6}>
                  <Tag className="font-mono-tight">{c.name}</Tag>
                  {level && (
                    <Tag color={level === "dangerous" ? "error" : "warning"}>{level}</Tag>
                  )}
                </Space>
                <pre
                  className="font-mono-tight"
                  style={{
                    margin: "4px 0 0",
                    padding: "6px 8px",
                    background: "rgba(128,128,128,0.08)",
                    borderRadius: 4,
                    fontSize: 12,
                    whiteSpace: "pre-wrap",
                    maxHeight: 200,
                    overflow: "auto",
                  }}
                >
                  {JSON.stringify(c.args, null, 2)}
                </pre>
              </div>
            );
          })}
        </div>
      )}

      <div style={{ display: "flex", gap: 8 }}>
        <Button
          type="primary"
          icon={<CheckOutlined />}
          onClick={onConfirm}
          danger={hasDangerous || isHighRisk}
        >
          {isHighRisk ? "确认执行" : "批准计划"}
        </Button>
        <Button icon={<CloseOutlined />} onClick={onReject}>
          拒绝
        </Button>
      </div>
    </Card>
  );
}
