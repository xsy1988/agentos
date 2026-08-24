/**
 * 确认卡片（前端设计 §3.1 确认卡片非模态）。
 * 出现在输入框上方而非全屏遮罩；dangerous 工具红色强调。
 */
import { Card, Button, Typography, Tag, Space } from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  ExclamationCircleOutlined,
} from "@ant-design/icons";

export default function ConfirmCard({
  payload,
  onConfirm,
  onReject,
}: {
  payload: Record<string, unknown>;
  onConfirm: () => void;
  onReject: () => void;
}) {
  const kind = payload.kind as string;
  const isToolConfirm = kind === "confirm_tool";
  const isDangerous = (payload.risk_level as string) === "dangerous";

  const title =
    kind === "confirm_plan" ? "请确认计划" :
    kind === "confirm_tool" ? "提交前请确认参数" : "需要确认";

  const plan = payload.plan as string[] | undefined;
  const toolName = payload.tool_name as string | undefined;
  const toolArgs = payload.tool_args ?? payload.args;

  return (
    <Card
      size="small"
      style={{
        margin: "0 16px 8px",
        borderColor: isDangerous ? "var(--ant-color-error)" : "var(--ant-color-warning)",
        borderWidth: isDangerous ? 2 : 1,
        background: isDangerous
          ? "rgba(255,77,79,0.06)"
          : "rgba(250,173,20,0.06)",
      }}
    >
      <Space size={6} style={{ marginBottom: 8 }}>
        {isDangerous ? (
          <ExclamationCircleOutlined style={{ color: "var(--ant-color-error)" }} />
        ) : null}
        <Typography.Text strong>
          {isDangerous ? "高危操作" : title}
        </Typography.Text>
        {isToolConfirm && toolName && (
          <Tag className="font-mono-tight">{toolName}</Tag>
        )}
      </Space>

      {plan && (
        <div style={{ marginBottom: 8 }}>
          {plan.map((step, i) => (
            <div key={i} style={{ fontSize: 13, marginBottom: 2 }}>
              {i + 1}. {step}
            </div>
          ))}
        </div>
      )}

      {toolArgs != null && (
        <pre
          className="font-mono-tight"
          style={{
            margin: "0 0 8px 0",
            padding: "6px 8px",
            background: "rgba(128,128,128,0.08)",
            borderRadius: 4,
            fontSize: 12,
            whiteSpace: "pre-wrap",
            maxHeight: 200,
            overflow: "auto",
          }}
        >
          {JSON.stringify(toolArgs, null, 2)}
        </pre>
      )}

      <div style={{ display: "flex", gap: 8 }}>
        <Button
          type="primary"
          icon={<CheckOutlined />}
          onClick={onConfirm}
          danger={isDangerous}
        >
          {isDangerous ? "确认执行" : "确认"}
        </Button>
        <Button icon={<CloseOutlined />} onClick={onReject}>
          修改
        </Button>
      </div>
    </Card>
  );
}
