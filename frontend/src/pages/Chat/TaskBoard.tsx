/**
 * 任务看板（前端设计 §3.1 左栏）。
 *
 * 全部任务平铺单层列表：按创建时间倒序（最新在上），不按 Worker 分组。
 * 新建会话零选择：直接建会话（后端惰性落内建「通用任务」），
 * Agent 根据用户在会话中发的首条消息自动判定并改绑主任务 Worker。
 */
import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Button,
  Empty,
  Input,
  Popconfirm,
  Segmented,
  Space,
  Tag,
  Tooltip,
  Typography,
  message as antdMessage,
} from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
  LoadingOutlined,
  PauseCircleOutlined,
  PlusOutlined,
  SearchOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { conversationsApi } from "@/api/conversations";
import { tasksApi } from "@/api/tasks";
import type { TaskOut } from "@/api/types";
import FormDrawer from "@/components/FormDrawer";
import { deriveTaskState, TASK_STATE_LABELS } from "./taskDisplay";

type FilterKey = "all" | "active" | "awaiting" | "done";

const FILTERS: { value: FilterKey; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "active", label: "进行中" },
  { value: "awaiting", label: "待确认" },
  { value: "done", label: "已完成" },
];

function matches(t: TaskOut, filter: FilterKey): boolean {
  if (filter === "active") return t.status === "active";
  if (filter === "awaiting") return t.awaiting_confirm;
  if (filter === "done") return t.status === "done";
  return true;
}

/** 标题前状态 icon（五态线性）：已完成/执行中/等待中/已终止一律灰色弱化，
 *  仅待决策鲜艳橙；派生逻辑见 deriveTaskState，细节进 tooltip */
function taskStatusIcon(task: TaskOut): { node: ReactNode; tooltip: string } {
  const state = deriveTaskState(task);
  const gray = { color: "var(--ant-color-text-tertiary)" };
  switch (state) {
    case "awaiting":
      return {
        node: (
          <ExclamationCircleOutlined style={{ color: "var(--ant-color-warning)" }} />
        ),
        tooltip:
          task.awaiting_steps_count > 0
            ? `${task.awaiting_steps_count} 个子任务待你决策`
            : "有事项待你决策",
      };
    case "running":
      return {
        node: (
          <LoadingOutlined spin style={{ color: "var(--ant-color-text-secondary)" }} />
        ),
        tooltip: TASK_STATE_LABELS.running,
      };
    case "waiting":
      return {
        node: (
          <PauseCircleOutlined style={{ color: "var(--ant-color-text-quaternary)" }} />
        ),
        tooltip: TASK_STATE_LABELS.waiting,
      };
    case "terminated":
      return {
        node: <CloseCircleOutlined style={gray} />,
        tooltip: TASK_STATE_LABELS.terminated,
      };
    case "done":
    default:
      return {
        node: <CheckCircleOutlined style={gray} />,
        tooltip: TASK_STATE_LABELS.done,
      };
  }
}

/** 相对时间戳（WorkBuddy 式“16 小时前”） */
function timeAgo(iso: string): string {
  const min = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} 小时前`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d} 天前`;
  return new Date(iso).toLocaleDateString();
}

export default function TaskBoard({
  activeTaskId,
  onSelect,
  onTaskDeleted,
}: {
  activeTaskId: string | null;
  /** 选中任务：父页据此切换会话（会话 id 在 task.conversation 上） */
  onSelect: (task: TaskOut, runId?: string | null) => void;
  /** 删除的是当前任务时通知父页清空选中 */
  onTaskDeleted?: (taskId: string) => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<FilterKey>("all");
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null);

  const { data: tasks = [], isLoading } = useQuery({
    queryKey: ["tasks", "list"],
    queryFn: () => tasksApi.list({ limit: 200 }),
    refetchInterval: 20_000,
  });

  const renameMutation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      tasksApi.update(id, { title }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      setRenaming(null);
      antdMessage.success("任务已重命名");
    },
    onError: () => antdMessage.error("重命名失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => tasksApi.del(id),
    onSuccess: (_, id) => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      if (id === activeTaskId) onTaskDeleted?.(id);
      antdMessage.success("任务已删除（会话与消息保留）");
    },
    onError: () => antdMessage.error("删除失败"),
  });

  // 新建会话：零选择，后端建会话 + 内建通用任务实例；刷新列表后直接进入。
  // 注意不能用 fetchQuery（与看板 20s 轮询共享 queryKey，会复用建会话前发起的
  // in-flight 请求拿到旧列表 → find 不到新任务 → 不跳转）：直接调 API 绕过缓存。
  const createMutation = useMutation({
    mutationFn: () => conversationsApi.create("新会话"),
    onSuccess: async (conv) => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      const list = await tasksApi.list({ limit: 200 });
      const task = list.find((t) => t.conversation?.id === conv.id) ?? null;
      if (task) onSelect(task);
    },
    onError: (e: Error) => antdMessage.error(e.message || "新建会话失败"),
  });

  // 新建会话（需求 1）：先查是否已有「空的通用会话」（无消息），有则直接跳转复用，
  // 避免堆积空会话；没有才真正建会话。tasks 来自看板 useQuery（20s 轮询 + SSE invalidate）。
  const handleNewSession = () => {
    const empty = [...tasks]
      .filter(
        (t) =>
          (t.worker_name === "" || t.worker_name === "__common__") &&
          (t.conversation?.message_count ?? 0) === 0,
      )
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
    if (empty) {
      onSelect(empty);
      return;
    }
    createMutation.mutate();
  };

  // 平铺：创建时间倒序（最新在上）
  const visible = useMemo(() => {
    const kw = search.trim().toLowerCase();
    return [...tasks]
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .filter(
        (t) =>
          matches(t, filter) &&
          (!kw ||
            t.title.toLowerCase().includes(kw) ||
            t.worker_display_name.toLowerCase().includes(kw)),
      );
  }, [tasks, filter, search]);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: 10, display: "flex", flexDirection: "column", gap: 8 }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          block
          loading={createMutation.isPending}
          onClick={handleNewSession}
        >
          新建会话
        </Button>
        <Input
          size="small"
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜索任务"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <Segmented
          size="small"
          block
          value={filter}
          onChange={(v) => setFilter(v as FilterKey)}
          options={FILTERS}
        />
      </div>

      <div style={{ flex: 1, overflow: "auto" }}>
        {visible.map((task) => {
          const active = task.id === activeTaskId;
          const status = taskStatusIcon(task);
          return (
            <div
              key={task.id}
              className={`board-task-item${active ? " active" : ""}`}
              onClick={() => onSelect(task)}
            >
              {/* 单行紧凑：线形状态 icon + 名称 + 相对时间 + 操作（hover）。
                  进度条已按需求移除；进度数字/类型/越界收进 hover 展开行 */}
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Tooltip title={status.tooltip}>
                  <span className="board-task-status">{status.node}</span>
                </Tooltip>
                <Typography.Text
                  ellipsis
                  style={{ fontSize: 13, flex: 1, minWidth: 0 }}
                  strong={active}
                >
                  {task.title}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 11, flex: "none" }}>
                  {timeAgo(task.created_at)}
                </Typography.Text>
                {/* 操作按钮：默认隐藏，仅 hover 才展开（选中但未 hover 也不显示；
                    CSS 控制，勿用内联 visibility） */}
                <Space
                  size={0}
                  className="board-task-actions"
                  style={{ flex: "none" }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <Button
                    type="text"
                    size="small"
                    icon={<EditOutlined />}
                    aria-label="重命名任务"
                    onClick={() =>
                      setRenaming({ id: task.id, title: task.title })
                    }
                  />
                  <Popconfirm
                    title="删除此任务？"
                    description="子任务清单将一并删除；会话与消息保留。"
                    okText="删除"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() => deleteMutation.mutate(task.id)}
                  >
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                      aria-label="删除任务"
                    />
                  </Popconfirm>
                </Space>
              </div>

              {/* 辅助信息（仅 hover 才展开）：进度数字 + 类型 + 越界 */}
              <div className="board-task-meta">
                {task.progress_total > 0 && (
                  <span className="board-task-type">
                    {task.progress_done}/{task.progress_total}
                  </span>
                )}
                <span className="board-task-type">{task.worker_display_name}</span>
                {task.out_of_scope_count > 0 && (
                  <Tooltip title="本会话内执行了不属于该主任务的请求">
                    <Tag className="board-tag-mini" style={{ marginInlineEnd: 0 }}>
                      越界 {task.out_of_scope_count}
                    </Tag>
                  </Tooltip>
                )}
              </div>
            </div>
          );
        })}

        {!isLoading && visible.length === 0 && (
          <div style={{ padding: 24 }}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                <span style={{ fontSize: 12, color: "var(--ant-color-text-tertiary)" }}>
                  没有匹配的任务
                </span>
              }
            />
          </div>
        )}
      </div>

      <div
        style={{
          borderTop: "1px solid var(--ant-color-border-secondary)",
          padding: 6,
        }}
      >
        <Button
          type="text"
          size="small"
          block
          icon={<SettingOutlined />}
          style={{ fontSize: 12, color: "var(--ant-color-text-secondary)" }}
          onClick={() => navigate("/capabilities/workers")}
        >
          管理 Worker
        </Button>
      </div>

      <FormDrawer
        title="重命名任务"
        open={!!renaming}
        onClose={() => setRenaming(null)}
        onOk={() => renaming && renaming.title.trim() && renameMutation.mutate(renaming)}
        confirmLoading={renameMutation.isPending}
        okText="保存"
        cancelText="取消"
        width={420}
        destroyOnClose
      >
        <Input
          value={renaming?.title ?? ""}
          maxLength={255}
          placeholder="任务名称"
          onChange={(e) =>
            setRenaming((prev) => (prev ? { ...prev, title: e.target.value } : prev))
          }
          onPressEnter={() =>
            renaming && renaming.title.trim() && renameMutation.mutate(renaming)
          }
        />
      </FormDrawer>
    </div>
  );
}
