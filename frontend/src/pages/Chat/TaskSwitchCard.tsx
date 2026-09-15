/**
 * 「这像是一个新的主任务」提示卡（ADR-27 软提示，非阻断）。
 *
 * 后端 send_message 返回 kind=task_switch_suggested 时展示：
 * 选择「新开会话并发送」= 用它建议的模板新建任务实例并发送原消息；
 * 选择「仍在本会话继续」= 带 force_current_task 重发，并记录越界。
 */
import { Alert, Button, Card, Space, Typography } from "antd";
import { PlusOutlined, RollbackOutlined } from "@ant-design/icons";
import type { SendMessageTaskSwitch } from "@/api/types";

export default function TaskSwitchCard({
  suggestion,
  onNewSession,
  onContinueHere,
  busy,
}: {
  suggestion: SendMessageTaskSwitch;
  onNewSession: () => void;
  onContinueHere: () => void;
  busy?: boolean;
}) {
  return (
    <Card
      size="small"
      style={{
        margin: "0 16px 8px",
        borderColor: "var(--ant-color-warning)",
        background: "rgba(250,173,20,0.06)",
      }}
    >
      <Space size={6} style={{ marginBottom: 6 }}>
        <Typography.Text strong>这像是一个新的主任务</Typography.Text>
      </Space>
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 8 }}
        message={`这条消息看起来属于「${suggestion.suggested_task_type.task_type_name}」`}
        description={
          <span style={{ fontSize: 12 }}>
            {suggestion.suggested_task_type.reason}。当前会话正在执行「
            {suggestion.current_task_type_name}」，一个会话只承载一个主任务；建议新开会话执行。
          </span>
        }
      />
      <div
        style={{
          fontSize: 12,
          color: "rgba(128,128,128,0.9)",
          marginBottom: 8,
          maxHeight: 80,
          overflow: "auto",
          whiteSpace: "pre-wrap",
        }}
      >
        「{suggestion.pending_text}」
      </div>
      <Space>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          loading={busy}
          onClick={onNewSession}
        >
          新开会话并发送
        </Button>
        <Button icon={<RollbackOutlined />} disabled={busy} onClick={onContinueHere}>
          仍在本会话继续
        </Button>
      </Space>
    </Card>
  );
}
