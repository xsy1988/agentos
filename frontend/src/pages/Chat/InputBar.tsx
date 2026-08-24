/**
 * 消息输入栏（前端设计 §3.1 底部）。
 * Markdown 编辑模式、发送、终止进行中的 run。
 */
import { useState } from "react";
import { Input, Button, Space } from "antd";
import { SendOutlined, StopOutlined } from "@ant-design/icons";
import { runsApi } from "@/api/runs";

export default function InputBar({
  onSend,
  disabled,
  runId,
}: {
  onSend: (text: string) => Promise<void>;
  disabled: boolean;
  runId?: string;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  const handleSend = async () => {
    if (!text.trim() || disabled || sending) return;
    setSending(true);
    try {
      await onSend(text.trim());
      setText("");
    } finally {
      setSending(false);
    }
  };

  const handleAbort = async () => {
    if (!runId) return;
    await runsApi.abort(runId);
  };

  return (
    <div
      style={{
        padding: "8px 16px",
        borderTop: "1px solid rgba(128,128,128,0.2)",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-end", gap: 8 }}>
        <Input.TextArea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="输入消息…（Shift+Enter 换行，Enter 发送）"
          autoSize={{ minRows: 1, maxRows: 6 }}
          disabled={disabled}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          style={{ flex: 1, resize: "none" }}
        />
        <Space direction="vertical" size={0}>
          {disabled && runId ? (
            <Button
              danger
              icon={<StopOutlined />}
              onClick={handleAbort}
              size="large"
            >
              终止
            </Button>
          ) : (
            <Button
              type="primary"
              icon={<SendOutlined />}
              onClick={handleSend}
              loading={sending}
              disabled={disabled || !text.trim()}
              size="large"
            >
              发送
            </Button>
          )}
        </Space>
      </div>
    </div>
  );
}
