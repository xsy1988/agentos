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
import { runsApi } from "@/api/runs";
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

  // SSE 回调：处理确认中断（后端事件名为 confirmation_request，
  // payload 结构 {reason: plan_review|high_risk_tool, payload: {...}}）
  const handleSSEEvent = useCallback(
    (event: { event_type: string; run_id?: string; payload: Record<string, unknown> }) => {
      if (event.event_type === "confirmation_request") {
        setConfirming({
          runId: (event.run_id as string) || "",
          payload: event.payload,
        });
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

  // 发送消息（modelProviderId：对话内临时换模型；attachmentIds：附件；
  // confirmUpload：图片外发涉密确认，由 InputBar 的确认弹窗触发，均可选）
  const handleSend = async (
    text: string,
    modelProviderId?: string,
    attachmentIds?: string[],
    confirmUpload?: boolean,
  ) => {
    if (!activeConvId) return;
    const result = await conversationsApi.sendMessage(
      activeConvId,
      text,
      modelProviderId,
      attachmentIds,
      confirmUpload,
    );
    setActiveRun({ runId: result.run_id, status: "running" });
    setCtxRun(result.run_id);
    // 用户消息立即上屏：不等 run 终态（run 可能停在等待确认，届时才刷新就太晚）
    queryClient.invalidateQueries({ queryKey: ["messages", activeConvId] });
    // 会话列表排序（last_message_at）同步刷新
    queryClient.invalidateQueries({ queryKey: ["conversations"] });
  };

  // 确认卡片操作
  const handleConfirm = async (answer: "approved" | "rejected") => {
    if (!confirming) return;
    await runsApi.confirm(confirming.runId, answer);
    setConfirming(null);
  };

  // 选择会话：若最近有暂停待确认的 run，恢复关注它。
  // SSE 从 seq=0 重放全部事件（store 按 seq 去重），
  // confirmation_request 事件重放时会自动重新弹确认卡片。
  const handleSelectConversation = useCallback(
    async (convId: string) => {
      setActiveConvId(convId);
      setActiveRun(null);
      setConfirming(null);
      try {
        const paused = await runsApi.list({
          conversation_id: convId,
          status: "paused_awaiting_confirm",
          limit: 1,
        });
        if (paused.length > 0) {
          setActiveRun({ runId: paused[0].id, status: "paused_awaiting_confirm" });
          setCtxRun(paused[0].id);
        }
      } catch {
        // 恢复失败不阻断会话浏览
      }
    },
    [setCtxRun],
  );

  // 会话被删除：若删的是当前会话，清空选中与运行态，回到空态
  const handleConversationDeleted = useCallback(
    (id: string) => {
      if (id === activeConvId) {
        setActiveConvId(null);
        setActiveRun(null);
        setConfirming(null);
        setCtxRun(null);
      }
    },
    [activeConvId, setCtxRun],
  );

  return (
    <Layout style={{ height: "100%", background: "transparent" }}>
      <Sider
        width={240}
        style={{
          background: "var(--ant-color-bg-container)",
          borderRight: "1px solid rgba(128,128,128,0.2)",
          overflow: "auto",
          height: "100%",
        }}
      >
        <ConversationList
          activeId={activeConvId}
          onSelect={handleSelectConversation}
          onDeleted={handleConversationDeleted}
        />
      </Sider>
      <Content style={{ display: "flex", flexDirection: "column", overflow: "hidden", minHeight: 0 }}>
        {reconnecting && (
          <Alert
            type="warning"
            message="连接中断，重连中…"
            banner
            style={{ padding: "2px 16px", fontSize: 12, flex: "none" }}
          />
        )}
        {activeConvId ? (
          <>
            <div style={{ flex: 1, overflow: "auto", minHeight: 0 }}>
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
