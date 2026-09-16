/**
 * 对话页（前端设计 §3.1，最重的页面，工作台风格）。
 *
 * 布局：任务看板（左，替代原会话流水，ADR-23） | 任务条 + 消息流（SSE）+ 输入栏
 * 关键能力：SSE 流式渲染、思考/工具折叠态、任务进度、支线子任务确认、新主任务软提示、断线重连
 */
import { useState, useCallback, useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { Layout, Alert, message as antdMessage } from "antd";
import { MessageOutlined } from "@ant-design/icons";
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
import CardRenderer from "./CardRenderer";
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
  // 乐观用户消息（发送即上屏）：Kimi 式即时反馈，真实消息落库后 MessageStream 自动接管
  const [optimistic, setOptimistic] = useState<{ text: string; key: string } | null>(null);
  const queryClient = useQueryClient();
  const { setActiveRun: setCtxRun, reconnecting } = useSSEStore();
  const openContextPanel = useUIStore((s) => s.openContextPanel);
  const openSidebar = useUIStore((s) => s.openSidebar);

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
        // 侧边栏 plugin 前端直接经 runsApi.confirm 回传（不经本页 handleConfirm），
        // 故靠 run_status 恢复运行时清除确认卡（paused 态保留，等用户处理）
        if (status !== "paused_awaiting_confirm") setConfirming(null);
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
    // 乐观上屏：不等后端落库，消息立即出现在消息流（提交卡死感反馈的核心）
    setOptimistic({ text, key: `optimistic-${Date.now()}` });
    let result;
    try {
      result = await conversationsApi.sendMessage(
        activeConvId,
        text,
        modelProviderId,
        attachmentIds,
        confirmUpload,
        forceCurrentTask,
      );
    } catch (e) {
      // 发送失败：撤回乐观气泡并向上抛（InputBar 捕获后保留输入内容/处理 428 确认门）
      setOptimistic(null);
      throw e;
    }
    // 软提示：不落消息不建 run，弹提示卡由用户决定（ADR-27）
    if (result.kind === "task_switch_suggested") {
      setOptimistic(null); // 消息未落库，内容由提示卡的 pending_text 展示
      setSuggestion(result);
      return;
    }
    setSuggestion(null);
    setActiveRun({ runId: result.run_id, status: "running" });
    setCtxRun(result.run_id);
    // 任务详情侧边栏不自动打开（仅用户点「任务详情/详情」才开）：
    // 自动弹出抢占主区视线，用户抱怨过；执行反馈已在消息流内联呈现
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
  // 若最近有未终态 run（running / 待确认），一并恢复关注 —— 否则切走再切回，
  // 执行中的 run 会凭空消失（LiveRun 不渲染、SSE 断开），像是“信息被清空”。
  const handleSelectTask = useCallback(
    async (task: TaskOut, runId?: string | null) => {
      setActiveTaskId(task.id);
      setSuggestion(null);
      setOptimistic(null);
      const convId = task.conversation?.id ?? null;
      if (!convId) return;
      setActiveConvId(convId);
      setConfirming(null);
      // 恢复未终态 run：优先显式 runId，其次看板返回的 active_run_id
      // （后端 NON_TERMINAL_RUN_STATUSES：pending / running / paused_awaiting_confirm）。
      // 恢复后 useSSE 以 after=0 重放全部事件，执行过程/确认卡都能重建。
      const resumeRunId = runId ?? task.active_run_id ?? null;
      if (resumeRunId) {
        setActiveRun({ runId: resumeRunId, status: "running" });
        setCtxRun(resumeRunId);
      } else {
        setActiveRun(null);
      }
      queryClient.invalidateQueries({ queryKey: ["task", task.id] });
    },
    [setCtxRun, queryClient],
  );

  // 提示卡「新开会话并发送」：用建议 Worker 新建任务实例并带上原消息
  const handleSwitchNewSession = async () => {
    if (!suggestion) return;
    const s = suggestion;
    setSwitching(true);
    try {
      const res = await tasksApi.create({
        worker_name: s.suggested_worker.worker_name,
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
      } else {
        setActiveRun(null);
      }
      antdMessage.success(`已新建主任务「${res.task.worker_display_name}」`);
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
        setOptimistic(null);
        setCtxRun(null);
      }
    },
    [activeTaskId, setCtxRun],
  );

  // 深链 /chat?task=<id>（任务管理页「去会话」）：选中该任务并进入其会话，
  // 消费后 replace 清参，避免刷新/后退时重复触发
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const taskParam = searchParams.get("task");
    if (!taskParam) return;
    tasksApi
      .get(taskParam)
      .then((t) => handleSelectTask(t))
      .catch(() => antdMessage.error("任务不存在或已被删除"))
      .finally(() => setSearchParams({}, { replace: true }));
  }, [searchParams, setSearchParams, handleSelectTask]);

  return (
    <Layout style={{ height: "100%", background: "transparent" }}>
      <Sider
        width={272}
        style={{
          background: "var(--ant-color-bg-container)",
          borderRight: "1px solid var(--ant-color-border-secondary)",
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
                optimistic={optimistic}
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
              <CardRenderer
                payload={confirming.payload}
                runId={confirming.runId}
                onConfirm={(answer) => handleConfirm(answer ?? "approved")}
                onReject={() => handleConfirm("rejected")}
                onOpenSidebar={openSidebar}
              />
            )}
            <InputBar
              onSend={handleSend}
              disabled={!!activeRun && activeRun.status === "running"}
              runId={activeRun?.runId}
            />
          </>
        ) : (
          <div className="chat-empty" style={{ flex: 1 }}>
            <MessageOutlined style={{ fontSize: 40, color: "var(--ant-color-primary)", opacity: 0.35 }} />
            <div className="chat-empty-title">开始一段新的对话</div>
            <div className="chat-empty-sub">
              点击左侧「新建会话」，直接描述你要做的事
            </div>
            <div className="chat-empty-sub" style={{ fontSize: 12 }}>
              Agent 会根据你的消息自动判定任务类型，无需预先选择
            </div>
          </div>
        )}
      </Content>
    </Layout>
  );
}
