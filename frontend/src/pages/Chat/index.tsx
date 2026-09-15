/**
 * 对话页（前端设计 §3.1，最重的页面，工作台风格）。
 *
 * 布局：任务看板（左，替代原会话流水，ADR-23） | 任务条 + 消息流（SSE）+ 输入栏
 * 关键能力：SSE 流式渲染、思考/工具折叠态、任务进度、支线子任务确认、新主任务软提示、断线重连
 */
import { useState, useCallback } from "react";
import { Layout, Alert, message as antdMessage } from "antd";
import { useQueryClient } from "@tanstack/react-query";
import { conversationsApi } from "@/api/conversations";
import { runsApi } from "@/api/runs";
import { tasksApi } from "@/api/tasks";
import type { SendMessageTaskSwitch, TaskOut } from "@/api/types";
import { useSSE } from "@/hooks/useSSE";
import { useSSEStore } from "@/store/sse";
import { useUIStore } from "@/store/ui";
import TaskBoard from "./TaskBoard";
import TaskHeader from "./TaskHeader";
import MessageStream from "./MessageStream";
import InputBar from "./InputBar";
import ConfirmCard from "./ConfirmCard";
import TaskSwitchCard from "./TaskSwitchCard";

const { Sider, Content } = Layout;

export interface ActiveRun {
  runId: string;
  status: string;
}

export default function ChatPage() {
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);
  const [confirming, setConfirming] = useState<{
    runId: string;
    payload: Record<string, unknown>;
  } | null>(null);
  // 新主任务软提示（ADR-27）：不落消息不建 run，等用户拍板
  const [suggestion, setSuggestion] = useState<SendMessageTaskSwitch | null>(null);
  const [switching, setSwitching] = useState(false);
  const queryClient = useQueryClient();
  const { setActiveRun: setCtxRun, reconnecting } = useSSEStore();
  const openContextPanel = useUIStore((s) => s.openContextPanel);

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
      // 任务架构（ADR-25）：进度与支线变化复用 plan_updated 事件族，
      // 前端只需让任务看板/任务条重新取数，不新增事件类型
      if (
        ["plan_updated", "confirmation_request"].includes(event.event_type) ||
        event.event_type === "run_status"
      ) {
        queryClient.invalidateQueries({ queryKey: ["tasks"] });
        if (activeTaskId) {
          queryClient.invalidateQueries({ queryKey: ["task", activeTaskId] });
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
    [activeConvId, activeTaskId, queryClient],
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
    forceCurrentTask?: boolean,
  ) => {
    if (!activeConvId) return;
    const result = await conversationsApi.sendMessage(
      activeConvId,
      text,
      modelProviderId,
      attachmentIds,
      confirmUpload,
      forceCurrentTask,
    );
    // 软提示：不落消息不建 run，弹提示卡由用户决定（ADR-27）
    if (result.kind === "task_switch_suggested") {
      setSuggestion(result);
      return;
    }
    setSuggestion(null);
    setActiveRun({ runId: result.run_id, status: "running" });
    setCtxRun(result.run_id);
    // 发送消息 = 具体事件：自动展开右栏任务详情
    openContextPanel();
    // 用户消息立即上屏：不等 run 终态（run 可能停在等待确认，届时才刷新就太晚）
    queryClient.invalidateQueries({ queryKey: ["messages", activeConvId] });
    // 会话列表排序（last_message_at）同步刷新
    queryClient.invalidateQueries({ queryKey: ["conversations"] });
  };

  // 确认卡片操作：approved/rejected（计划、高危工具）或支线子任务的文本答复（ADR-24）
  const handleConfirm = async (answer: string) => {
    if (!confirming) return;
    try {
      await runsApi.confirm(confirming.runId, answer);
    } catch (e) {
      antdMessage.error(e instanceof Error ? e.message : "提交失败");
      return;
    }
    setConfirming(null);
    // 答复回注后任务条/看板里的子任务会从 awaiting_user 转为 done
    queryClient.invalidateQueries({ queryKey: ["tasks"] });
    if (activeTaskId) queryClient.invalidateQueries({ queryKey: ["task", activeTaskId] });
    queryClient.invalidateQueries({ queryKey: ["messages", activeConvId] });
  };

  // 看板选中任务：进入该任务实例的会话（会话 id 在 task.conversation 上）。
  // 若最近有暂停待确认的 run，一并恢复关注。
  const handleSelectTask = useCallback(
    async (task: TaskOut, runId?: string | null) => {
      setActiveTaskId(task.id);
      setSuggestion(null);
      const convId = task.conversation?.id ?? null;
      if (!convId) return;
      setActiveConvId(convId);
      setActiveRun(runId ? { runId, status: "running" } : null);
      setConfirming(null);
      if (runId) {
        setCtxRun(runId);
        openContextPanel();
      }
      try {
        const paused = await runsApi.list({
          conversation_id: convId,
          status: "paused_awaiting_confirm",
          limit: 1,
        });
        if (paused.length > 0) {
          setActiveRun({ runId: paused[0].id, status: "paused_awaiting_confirm" });
          setCtxRun(paused[0].id);
          openContextPanel();
        }
      } catch {
        // 恢复失败不阻断任务浏览
      }
      queryClient.invalidateQueries({ queryKey: ["task", task.id] });
    },
    [setCtxRun, openContextPanel, queryClient],
  );

  // 提示卡「新开会话并发送」：用建议模板新建任务实例并带上原消息
  const handleSwitchNewSession = async () => {
    if (!suggestion) return;
    const s = suggestion;
    setSwitching(true);
    try {
      const res = await tasksApi.create({
        task_type_id: s.suggested_task_type.task_type_id,
        text: s.pending_text,
      });
      setSuggestion(null);
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      setActiveTaskId(res.task.id);
      setActiveConvId(res.conversation_id);
      if (res.run_id) {
        setActiveRun({ runId: res.run_id, status: "running" });
        setCtxRun(res.run_id);
        openContextPanel();
      } else {
        setActiveRun(null);
      }
      antdMessage.success(`已新建主任务「${res.task.task_type_name}」`);
    } catch (e) {
      antdMessage.error(e instanceof Error ? e.message : "新建任务失败");
    } finally {
      setSwitching(false);
    }
  };

  // 提示卡「仍在本会话继续」：带 force_current_task 重发，后端记录越界
  const handleContinueHere = async () => {
    if (!suggestion) return;
    const pending = suggestion.pending_text;
    setSuggestion(null);
    await handleSend(pending, undefined, undefined, undefined, true);
  };

  // 任务被删除：清空选中与其会话
  const handleTaskDeleted = useCallback(
    (taskId: string) => {
      if (taskId === activeTaskId) {
        setActiveTaskId(null);
        setActiveConvId(null);
        setActiveRun(null);
        setConfirming(null);
        setSuggestion(null);
        setCtxRun(null);
      }
    },
    [activeTaskId, setCtxRun],
  );

  return (
    <Layout style={{ height: "100%", background: "transparent" }}>
      <Sider
        width={260}
        style={{
          background: "var(--ant-color-bg-container)",
          borderRight: "1px solid rgba(128,128,128,0.2)",
          overflow: "auto",
          height: "100%",
        }}
      >
        <TaskBoard
          activeTaskId={activeTaskId}
          onSelect={handleSelectTask}
          onTaskDeleted={handleTaskDeleted}
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
            {activeTaskId && <TaskHeader taskId={activeTaskId} onOpenContext={openContextPanel} />}
            <div style={{ flex: 1, overflow: "auto", minHeight: 0 }}>
              <MessageStream
                convId={activeConvId}
                activeRun={activeRun}
              />
            </div>
            {suggestion && (
              <TaskSwitchCard
                suggestion={suggestion}
                busy={switching}
                onNewSession={handleSwitchNewSession}
                onContinueHere={handleContinueHere}
              />
            )}
            {confirming && (
              <ConfirmCard
                payload={confirming.payload}
                onConfirm={(answer) => handleConfirm(answer ?? "approved")}
                onReject={() => handleConfirm("rejected")}
              />
            )}
            <InputBar
              onSend={handleSend}
              disabled={!!activeRun && activeRun.status === "running"}
              runId={activeRun?.runId}
            />
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
              从左侧任务看板选择任务，或「新建主任务」开始
            </span>
          </div>
        )}
      </Content>
    </Layout>
  );
}
