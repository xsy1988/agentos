/**
 * 对话页（前端设计 §3.1，最重的页面，工作台风格）。
 *
 * 布局：会话列表 | 消息流（SSE 实时事件）+ 输入栏
 * 关键能力：SSE 流式渲染、思考/工具折叠态、计划进度、确认卡片、预算指示、断线重连
 */
import { useState, useCallback } from "react";
import { Layout, Alert } from "antd";
import { useQueryClient } from "@tanstack/react-query";
import { conversationsApi } from "@/api/conversations";
import { useSSE } from "@/hooks/useSSE";
import { useSSEStore } from "@/store/sse";
import ConversationList from "./ConversationList";
import MessageStream from "./MessageStream";
import InputBar from "./InputBar";
import ConfirmCard from "./ConfirmCard";

const { Sider, Content } = Layout;

export interface ActiveRun {
  runId: string;
  status: string;
}

export default function ChatPage() {
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);
  const [confirming, setConfirming] = useState<{
    runId: string;
    payload: Record<string, unknown>;
  } | null>(null);
  const queryClient = useQueryClient();
  const { setActiveRun: setCtxRun, reconnecting } = useSSEStore();

  // SSE 回调：处理确认中断
  const handleSSEEvent = useCallback(
    (event: { event_type: string; payload: Record<string, unknown> }) => {
      if (event.event_type === "interrupt") {
        const kind = event.payload.kind as string;
        if (kind === "confirm_plan" || kind === "confirm_tool") {
          setConfirming({
            runId: event.payload.run_id as string,
            payload: event.payload,
          });
        }
      }
      if (event.event_type === "run_status") {
        const status = event.payload.status as string;
        setActiveRun((prev) =>
          prev ? { ...prev, status } : null,
        );
        // 终态时刷新消息列表
        if (["done", "failed", "aborted", "timeout"].includes(status)) {
          if (activeConvId) {
            queryClient.invalidateQueries({
              queryKey: ["messages", activeConvId],
            });
          }
        }
      }
    },
    [activeConvId, queryClient],
  );

  // SSE 连接（当有 activeRun 时）
  useSSE(activeRun?.runId ?? null, { onEvent: handleSSEEvent });

  // 发送消息
  const handleSend = async (text: string) => {
    if (!activeConvId) return;
    const result = await conversationsApi.sendMessage(activeConvId, text);
    setActiveRun({ runId: result.run_id, status: "running" });
    setCtxRun(result.run_id);
  };

  // 确认卡片操作
  const handleConfirm = async (answer: "approved" | "rejected") => {
    if (!confirming) return;
    await import("@/api/runs").then(({ runsApi }) =>
      runsApi.confirm(confirming.runId, answer),
    );
    setConfirming(null);
  };

  // 选择会话
  const handleSelectConversation = useCallback((convId: string) => {
    setActiveConvId(convId);
    setActiveRun(null);
  }, []);

  return (
    <Layout style={{ height: "100%", background: "transparent" }}>
      <Sider
        width={240}
        style={{
          background: "var(--ant-color-bg-container)",
          borderRight: "1px solid rgba(128,128,128,0.2)",
          overflow: "auto",
        }}
      >
        <ConversationList
          activeId={activeConvId}
          onSelect={handleSelectConversation}
        />
      </Sider>
      <Content style={{ display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {reconnecting && (
          <Alert
            type="warning"
            message="连接中断，重连中…"
            banner
            style={{ padding: "2px 16px", fontSize: 12 }}
          />
        )}
        {activeConvId ? (
          <>
            <div style={{ flex: 1, overflow: "auto" }}>
              <MessageStream
                convId={activeConvId}
                activeRun={activeRun}
              />
            </div>
            {confirming && (
              <ConfirmCard
                payload={confirming.payload}
                onConfirm={() => handleConfirm("approved")}
                onReject={() => handleConfirm("rejected")}
              />
            )}
            <InputBar onSend={handleSend} disabled={!!activeRun && activeRun.status === "running"} runId={activeRun?.runId} />
          </>
        ) : (
          <div
            style={{
              flex: 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <span style={{ color: "rgba(128,128,128,0.5)" }}>
              选择左侧会话开始对话，或新建会话
            </span>
          </div>
        )}
      </Content>
    </Layout>
  );
}
