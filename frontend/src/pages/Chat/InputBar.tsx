/**
 * 消息输入栏（前端设计 §3.1 底部）。
 * Markdown 编辑模式、发送、终止进行中的 run。
 * 模型选择：run 级覆盖（随消息快照固化），默认跟随 Agent 绑定模型。
 */
import { useEffect, useState } from "react";
import { Input, Button, Space, Select, Tooltip } from "antd";
import { SendOutlined, StopOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { runsApi } from "@/api/runs";
import { modelsApi } from "@/api/models";
import type { ModelProviderOut } from "@/api/types";

const MODEL_PREF_KEY = "chat.modelProviderId";

export default function InputBar({
  onSend,
  disabled,
  runId,
}: {
  onSend: (text: string, modelProviderId?: string) => Promise<void>;
  disabled: boolean;
  runId?: string;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  // 选择持久化：切换会话/刷新后保留（localStorage）
  const [modelId, setModelId] = useState<string | undefined>(() =>
    localStorage.getItem(MODEL_PREF_KEY) || undefined,
  );

  useEffect(() => {
    if (modelId) localStorage.setItem(MODEL_PREF_KEY, modelId);
    else localStorage.removeItem(MODEL_PREF_KEY);
  }, [modelId]);

  const { data: models } = useQuery({
    queryKey: ["models", "llm"],
    queryFn: () => modelsApi.list("llm"),
    staleTime: 60_000,
  });
  const llms = (models ?? []).filter((m: ModelProviderOut) => m.status === "enabled");

  // 已选模型被删除/停用时自动回退「跟随 Agent」
  useEffect(() => {
    if (modelId && !llms.some((m) => m.id === modelId)) setModelId(undefined);
  }, [llms, modelId]);

  const handleSend = async () => {
    if (!text.trim() || disabled || sending) return;
    setSending(true);
    try {
      await onSend(text.trim(), modelId);
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
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          alignItems: "center",
          marginTop: 4,
          fontSize: 12,
          color: "rgba(128,128,128,0.8)",
        }}
      >
        <Tooltip title="本次对话使用的模型；「跟随 Agent」即使用 Agent 管理中绑定的模型">
          <span style={{ marginRight: 8 }}>模型</span>
        </Tooltip>
        <Select
          size="small"
          style={{ minWidth: 200 }}
          value={modelId ?? "follow-agent"}
          onChange={(v: string) => setModelId(v === "follow-agent" ? undefined : v)}
          options={[
            { value: "follow-agent", label: "跟随 Agent 默认" },
            ...llms.map((m) => ({ value: m.id, label: m.name })),
          ]}
        />
      </div>
    </div>
  );
}
