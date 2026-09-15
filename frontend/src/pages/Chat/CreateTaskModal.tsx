/**
 * 新建主任务弹窗（ADR-23）：选主任务模板 → 可填首条消息 → 后端一把建
 * 会话 + 任务实例（含子任务骨架）+ 首个 run，前端跳到会话页并订阅 SSE。
 */
import { useState } from "react";
import {
  Alert,
  Card,
  Empty,
  Input,
  Modal,
  Space,
  Tag,
  Typography,
  message as antdMessage,
} from "antd";
import { useMutation, useQuery } from "@tanstack/react-query";
import { taskTypesApi, tasksApi } from "@/api/tasks";
import type { TaskOut, TaskTypeOut } from "@/api/types";

export default function CreateTaskModal({
  onClose,
  onCreated,
  /** 预选模板（「新开会话并发送」时由提示卡带入） */
  defaultTaskTypeId,
  defaultText,
}: {
  onClose: () => void;
  onCreated: (task: TaskOut, runId: string | null) => void;
  defaultTaskTypeId?: string;
  defaultText?: string;
}) {
  const [taskTypeId, setTaskTypeId] = useState<string | null>(defaultTaskTypeId ?? null);
  const [title, setTitle] = useState("");
  const [text, setText] = useState(defaultText ?? "");

  const { data: types = [], isLoading } = useQuery({
    queryKey: ["task-types"],
    queryFn: taskTypesApi.list,
  });

  // 业务模板优先展示；「通用任务集」通常不需要用户手选，但保留可用
  const sorted = [...types].sort((a, b) =>
    a.kind === b.kind ? a.sort_order - b.sort_order : a.kind === "business" ? -1 : 1,
  );

  const createMutation = useMutation({
    mutationFn: () =>
      tasksApi.create({
        task_type_id: taskTypeId as string,
        title: title.trim() || null,
        text,
      }),
    onSuccess: (res) => {
      antdMessage.success("主任务已创建");
      onCreated(res.task, res.run_id);
    },
    onError: (e: Error) => antdMessage.error(e.message || "创建失败"),
  });

  const selected = sorted.find((t) => t.id === taskTypeId) ?? null;

  return (
    <Modal
      title="新建主任务"
      open
      width={640}
      onCancel={onClose}
      okText="创建并开始"
      cancelText="取消"
      confirmLoading={createMutation.isPending}
      okButtonProps={{ disabled: !taskTypeId }}
      onOk={() => createMutation.mutate()}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="一个主任务对应一个独立会话"
        description="主任务（任务集合）及其支线子任务都在同一个会话中进行；中途遇到需要你拍板的支线，会暂停并弹确认卡片。"
      />

      <Typography.Text strong style={{ fontSize: 13 }}>
        选择主任务
      </Typography.Text>
      <div
        style={{
          maxHeight: 260,
          overflow: "auto",
          margin: "8px 0 12px",
          display: "flex",
          flexDirection: "column",
          gap: 6,
        }}
      >
        {isLoading && <Typography.Text type="secondary">加载中…</Typography.Text>}
        {!isLoading && sorted.length === 0 && (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有主任务模板，请先到「任务模板」页创建"
          />
        )}
        {sorted.map((t: TaskTypeOut) => (
          <Card
            key={t.id}
            size="small"
            hoverable
            onClick={() => setTaskTypeId(t.id)}
            style={{
              borderColor:
                t.id === taskTypeId ? "var(--ant-color-primary)" : undefined,
              borderWidth: t.id === taskTypeId ? 2 : 1,
            }}
          >
            <Space size={6} style={{ marginBottom: 2 }}>
              <span>{t.icon || "📋"}</span>
              <Typography.Text strong>{t.name}</Typography.Text>
              {t.kind === "common" && <Tag style={{ fontSize: 11 }}>通用</Tag>}
            </Space>
            <div style={{ fontSize: 12, color: "rgba(128,128,128,0.9)" }}>
              {t.description || "（无描述）"}
            </div>
            {t.steps.length > 0 && (
              <div style={{ fontSize: 11, color: "rgba(128,128,128,0.75)", marginTop: 4 }}>
                子任务：
                {t.steps
                  .map((s) => (s.kind === "branch" ? `${s.name}(支线)` : s.name))
                  .join(" · ")}
              </div>
            )}
          </Card>
        ))}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <Input
          placeholder={`任务标题（可留空，默认「${selected?.name ?? "主任务名"}」）`}
          value={title}
          maxLength={255}
          onChange={(e) => setTitle(e.target.value)}
        />
        <Input.TextArea
          placeholder="首条消息（可留空，创建后也可随时发送）"
          value={text}
          rows={3}
          maxLength={32000}
          onChange={(e) => setText(e.target.value)}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          首条消息会作为该主任务的第一个 run 立刻执行。
        </Typography.Text>
      </div>
    </Modal>
  );
}
