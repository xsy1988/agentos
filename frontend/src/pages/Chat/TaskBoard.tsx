/**
 * 任务看板（前端设计 §3.1 左栏 = 原「对话记录」的新形态，ADR-23）。
 *
 * 一级 = 主任务分组（任务模板），二级 = 组内任务实例卡片：
 * 卡片显示任务名称、进度（确定性计算）、状态徽标、待确认角标、越界角标。
 * 点击卡片 = 进入该任务实例的会话（一个主任务一个会话）。
 */
import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  Empty,
  Input,
  List,
  Modal,
  Popconfirm,
  Progress,
  Segmented,
  Space,
  Tag,
  Tooltip,
  Typography,
  message as antdMessage,
} from "antd";
import {
  DeleteOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
  PlusOutlined,
  SearchOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { tasksApi } from "@/api/tasks";
import type { TaskOut } from "@/api/types";
import CreateTaskModal from "./CreateTaskModal";
import {
  TASK_STATUS_COLORS,
  TASK_STATUS_LABELS,
  progressText,
} from "./taskDisplay";

type FilterKey = "all" | "active" | "awaiting" | "done";

const FILTERS: { value: FilterKey; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "active", label: "进行中" },
  { value: "awaiting", label: "待确认" },
  { value: "done", label: "已完成" },
];

function matches(task: TaskOut, filter: FilterKey): boolean {
  if (filter === "all") return true;
  if (filter === "awaiting") return task.awaiting_confirm;
  if (filter === "active") return task.status === "active";
  return task.status === "done" || task.status === "cancelled";
}

export default function TaskBoard({
  activeTaskId,
  onSelect,
  onTaskDeleted,
}: {
  activeTaskId: string | null;
  /** 选中任务：父页据此切换会话（会话 id 在 task.conversation 上）；新建任务时附带首条 run */
  onSelect: (task: TaskOut, runId?: string | null) => void;
  /** 删除的是当前任务时通知父页清空选中 */
  onTaskDeleted?: (taskId: string) => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<FilterKey>("all");
  const [createOpen, setCreateOpen] = useState(false);
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null);
  // 分组折叠态：默认全部展开
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const { data: groups = [], isLoading } = useQuery({
    queryKey: ["tasks", "board"],
    queryFn: () => tasksApi.board({ include_empty: true, limit_per_group: 30 }),
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

  const visibleGroups = useMemo(() => {
    const kw = search.trim().toLowerCase();
    return groups
      .map((g) => ({
        ...g,
        tasks: g.tasks.filter(
          (t) =>
            matches(t, filter) &&
            (!kw ||
              t.title.toLowerCase().includes(kw) ||
              g.task_type.name.toLowerCase().includes(kw)),
        ),
      }))
      .filter((g) => g.tasks.length > 0);
  }, [groups, filter, search]);

  const totalVisible = visibleGroups.reduce((n, g) => n + g.tasks.length, 0);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: 8, display: "flex", flexDirection: "column", gap: 8 }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          block
          onClick={() => setCreateOpen(true)}
        >
          新建主任务
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
        {visibleGroups.map((group) => {
          const key = group.task_type.id;
          const open = !collapsed[key];
          return (
            <div key={key}>
              <div
                onClick={() => setCollapsed((c) => ({ ...c, [key]: open }))}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "4px 10px",
                  cursor: "pointer",
                  fontSize: 12,
                  fontWeight: 600,
                  color: "rgba(128,128,128,0.85)",
                }}
              >
                <span style={{ width: 10 }}>{open ? "▾" : "▸"}</span>
                <span>{group.task_type.icon || "📋"}</span>
                <Typography.Text
                  ellipsis
                  style={{ fontSize: 12, flex: 1, minWidth: 0 }}
                  strong
                >
                  {group.task_type.name}
                </Typography.Text>
                <span style={{ fontWeight: 400 }}>
                  {group.task_count}
                  {group.awaiting_confirm_count > 0 && (
                    <Badge
                      count={group.awaiting_confirm_count}
                      size="small"
                      style={{ marginLeft: 4 }}
                    />
                  )}
                </span>
              </div>
              {open && (
                <List
                  dataSource={group.tasks}
                  renderItem={(task) => (
                    <List.Item
                      style={{
                        padding: "8px 12px",
                        cursor: "pointer",
                        display: "block",
                        background:
                          task.id === activeTaskId ? "rgba(22,119,255,0.1)" : undefined,
                        borderLeft:
                          task.id === activeTaskId
                            ? "3px solid var(--ant-color-primary)"
                            : "3px solid transparent",
                      }}
                      onClick={() => onSelect(task)}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                        <Typography.Text
                          ellipsis
                          style={{ fontSize: 13, flex: 1, minWidth: 0 }}
                          strong={task.id === activeTaskId}
                        >
                          {task.title}
                        </Typography.Text>
                        <Space
                          size={0}
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

                      <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 2 }}>
                        <Progress
                          percent={task.progress_percent}
                          size="small"
                          showInfo={false}
                          style={{ flex: 1, margin: 0, minWidth: 0 }}
                        />
                        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                          {progressText(task)}
                        </Typography.Text>
                      </div>

                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 4,
                          marginTop: 4,
                          flexWrap: "wrap",
                        }}
                      >
                        {task.status !== "active" && (
                          <Tag
                            color={TASK_STATUS_COLORS[task.status]}
                            style={{ marginInlineEnd: 0, fontSize: 11 }}
                          >
                            {TASK_STATUS_LABELS[task.status] ?? task.status}
                          </Tag>
                        )}
                        {task.awaiting_confirm && (
                          <Tag
                            icon={<ExclamationCircleOutlined />}
                            color="warning"
                            style={{ marginInlineEnd: 0, fontSize: 11 }}
                          >
                            待确认{task.awaiting_steps_count > 0 ? ` ${task.awaiting_steps_count}` : ""}
                          </Tag>
                        )}
                        {task.out_of_scope_count > 0 && (
                          <Tooltip title="本会话内执行了不属于该主任务的请求">
                            <Tag style={{ marginInlineEnd: 0, fontSize: 11 }}>
                              越界 {task.out_of_scope_count}
                            </Tag>
                          </Tooltip>
                        )}
                        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                          {task.conversation?.message_count ?? 0} 条
                        </Typography.Text>
                      </div>
                    </List.Item>
                  )}
                />
              )}
            </div>
          );
        })}

        {!isLoading && totalVisible === 0 && (
          <div style={{ padding: 24 }}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                <span style={{ fontSize: 12, color: "rgba(128,128,128,0.6)" }}>
                  没有匹配的任务
                </span>
              }
            />
          </div>
        )}
      </div>

      <div style={{ borderTop: "1px solid rgba(128,128,128,0.15)", padding: 6 }}>
        <Button
          type="text"
          size="small"
          block
          icon={<SettingOutlined />}
          style={{ fontSize: 12, color: "rgba(128,128,128,0.85)" }}
          onClick={() => navigate("/task-types")}
        >
          管理主任务模板
        </Button>
      </div>

      {createOpen && (
        <CreateTaskModal
          onClose={() => setCreateOpen(false)}
          onCreated={(task, runId) => {
            setCreateOpen(false);
            queryClient.invalidateQueries({ queryKey: ["tasks"] });
            queryClient.invalidateQueries({ queryKey: ["conversations"] });
            onSelect(task, runId);
          }}
        />
      )}

      <Modal
        title="重命名任务"
        open={!!renaming}
        onCancel={() => setRenaming(null)}
        onOk={() => renaming && renaming.title.trim() && renameMutation.mutate(renaming)}
        confirmLoading={renameMutation.isPending}
        okText="保存"
        cancelText="取消"
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
      </Modal>
    </div>
  );
}
