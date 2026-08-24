/**
 * 记忆页（前端设计 §3.5）。
 * 按 kind 筛选浏览记忆 → 手改内容 → 手动触发每日整理。
 */
import { useState } from "react";
import { List, Tag, Typography, Button, Modal, Input, Select, Space, Empty, Popconfirm, message } from "antd";
import { EditOutlined, DeleteOutlined, ThunderboltOutlined, BookOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { memoryApi } from "@/api/memory";
import type { MemoryOut } from "@/api/types";

const KIND_META: Record<string, { color: string; label: string }> = {
  fact: { color: "blue", label: "事实" },
  preference: { color: "purple", label: "偏好" },
  episode: { color: "cyan", label: "经历" },
  skill: { color: "gold", label: "技能" },
};

export default function MemoryPage() {
  const queryClient = useQueryClient();
  const [kindFilter, setKindFilter] = useState<string | undefined>(undefined);
  const [editing, setEditing] = useState<MemoryOut | null>(null);
  const [editContent, setEditContent] = useState("");

  const { data: memories = [], isLoading } = useQuery({
    queryKey: ["memories", kindFilter],
    queryFn: () => memoryApi.list(kindFilter),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, content }: { id: string; content: string }) =>
      memoryApi.update(id, { content }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      message.success("记忆已更新");
      setEditing(null);
    },
    onError: () => message.error("更新失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => memoryApi.del(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      message.success("已删除");
    },
  });

  const consolidateMutation = useMutation({
    mutationFn: () => memoryApi.consolidate(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      message.success("整理任务已触发（后台执行中）");
    },
    onError: () => message.error("触发失败"),
  });

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Typography.Title level={5} style={{ margin: 0 }}>
          <BookOutlined /> 记忆
        </Typography.Title>
        <Space>
          <Popconfirm
            title="手动触发每日记忆整理？"
            description="将昨天的高频经历提炼为长期记忆"
            onConfirm={() => consolidateMutation.mutate()}
          >
            <Button icon={<ThunderboltOutlined />} loading={consolidateMutation.isPending}>
              触发整理
            </Button>
          </Popconfirm>
        </Space>
      </div>

      <div style={{ marginBottom: 8 }}>
        <Select
          style={{ width: 160 }}
          placeholder="按类型筛选"
          allowClear
          value={kindFilter}
          onChange={(v) => setKindFilter(v)}
          options={[
            { value: "fact", label: "事实" },
            { value: "preference", label: "偏好" },
            { value: "episode", label: "经历" },
            { value: "skill", label: "技能" },
          ]}
        />
      </div>

      <List
        loading={isLoading}
        dataSource={memories}
        locale={{ emptyText: <Empty description="暂无记忆" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        renderItem={(m) => {
          const km = KIND_META[m.kind] || { color: "default", label: m.kind };
          return (
            <List.Item
              actions={[
                <Button
                  key="edit"
                  size="small"
                  type="text"
                  icon={<EditOutlined />}
                  onClick={() => { setEditing(m); setEditContent(m.content); }}
                >
                  编辑
                </Button>,
                <Popconfirm key="del" title="删除此记忆？" onConfirm={() => deleteMutation.mutate(m.id)}>
                  <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                </Popconfirm>,
              ]}
            >
              <div style={{ flex: 1, overflow: "hidden" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <Tag color={km.color}>{km.label}</Tag>
                  <Typography.Text strong style={{ fontSize: 13 }}>{m.title}</Typography.Text>
                  {m.date && (
                    <Typography.Text type="secondary" style={{ fontSize: 11 }}>{m.date}</Typography.Text>
                  )}
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    ~{m.token_count} tokens
                  </Typography.Text>
                </div>
                <Typography.Paragraph ellipsis={{ rows: 2 }} style={{ margin: 0, fontSize: 12 }}>
                  {m.content}
                </Typography.Paragraph>
              </div>
            </List.Item>
          );
        }}
      />

      <Modal
        title={`编辑记忆: ${editing?.title ?? ""}`}
        open={editing !== null}
        onCancel={() => setEditing(null)}
        onOk={() => editing && updateMutation.mutate({ id: editing.id, content: editContent })}
        confirmLoading={updateMutation.isPending}
        width={600}
      >
        <Input.TextArea
          rows={10}
          value={editContent}
          onChange={(e) => setEditContent(e.target.value)}
        />
      </Modal>
    </div>
  );
}
