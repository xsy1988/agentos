/**
 * 会话列表（前端设计 §3.1 左栏）。
 * 按日期分组、新建会话、重命名/删除（hover 操作）。
 */
import { Button, List, Typography, Input, Space, Popconfirm, Modal, message as antdMessage } from "antd";
import { PlusOutlined, SearchOutlined, EditOutlined, DeleteOutlined } from "@ant-design/icons";
import { useState, useMemo } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { conversationsApi } from "@/api/conversations";
import type { ConversationOut } from "@/api/types";

function formatDateGroup(date: string | null): string {
  if (!date) return "未知";
  const d = new Date(date);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today.getTime() - 86400000);
  if (d >= today) return "今天";
  if (d >= yesterday) return "昨天";
  return d.toLocaleDateString("zh-CN", { month: "long", day: "numeric" });
}

export default function ConversationList({
  activeId,
  onSelect,
  onDeleted,
}: {
  activeId: string | null;
  onSelect: (id: string) => void;
  /** 删除的是当前会话时通知父页清空选中状态 */
  onDeleted?: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  // 重命名弹窗状态
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null);

  const { data: conversations = [] } = useQuery({
    queryKey: ["conversations"],
    queryFn: conversationsApi.list,
    refetchInterval: 30_000,
  });

  const createMutation = useMutation({
    mutationFn: () => conversationsApi.create("新会话"),
    onSuccess: (conv) => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      onSelect(conv.id);
    },
  });

  const renameMutation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      conversationsApi.update(id, { title }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      setRenaming(null);
    },
    onError: () => antdMessage.error("重命名失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => conversationsApi.del(id),
    onSuccess: (_, id) => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      // 当前会话被删：清空选中；否则仅列表刷新
      if (id === activeId) onDeleted?.(id);
      antdMessage.success("会话已删除");
    },
    onError: () => antdMessage.error("删除失败（含运行中任务的会话请稍后重试）"),
  });

  const filtered = useMemo(() => {
    if (!search) return conversations;
    return conversations.filter((c) =>
      c.title.toLowerCase().includes(search.toLowerCase()),
    );
  }, [conversations, search]);

  // 分组：排序键同后端——最后消息时间，没有则用创建时间（新建会话归「今天」）
  const groups = useMemo(() => {
    const map = new Map<string, ConversationOut[]>();
    for (const c of filtered) {
      const key = formatDateGroup(c.last_message_at ?? c.created_at);
      (map.get(key) ?? map.set(key, []).get(key))!.push(c);
    }
    return Array.from(map.entries());
  }, [filtered]);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "8px" }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          block
          loading={createMutation.isPending}
          onClick={() => createMutation.mutate()}
        >
          新建会话
        </Button>
      </div>
      <div style={{ padding: "0 8px 8px" }}>
        <Input
          size="small"
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜索会话"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <div style={{ flex: 1, overflow: "auto" }}>
        {groups.map(([group, items]) => (
          <div key={group}>
            <div
              style={{
                padding: "4px 12px",
                fontSize: 11,
                color: "rgba(128,128,128,0.6)",
                fontWeight: 600,
              }}
            >
              {group}
            </div>
            <List
              dataSource={items}
              renderItem={(conv) => (
                <List.Item
                  style={{
                    padding: "8px 12px",
                    cursor: "pointer",
                    background:
                      conv.id === activeId
                        ? "rgba(22,119,255,0.1)"
                        : undefined,
                    borderLeft:
                      conv.id === activeId
                        ? "3px solid var(--ant-color-primary)"
                        : "3px solid transparent",
                  }}
                  onClick={() => onSelect(conv.id)}
                >
                  <div style={{ width: "100%", overflow: "hidden" }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 4 }}>
                      <Typography.Text
                        ellipsis
                        style={{ fontSize: 13, flex: 1, minWidth: 0 }}
                        strong={conv.id === activeId}
                      >
                        {conv.title}
                      </Typography.Text>
                      <Space
                        size={0}
                        className="conv-item-actions"
                        style={{ flex: "none" }}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <Button
                          type="text"
                          size="small"
                          icon={<EditOutlined />}
                          aria-label="重命名会话"
                          onClick={() => setRenaming({ id: conv.id, title: conv.title })}
                        />
                        <Popconfirm
                          title="删除此会话？"
                          description="消息、任务记录与计划将一并删除，不可恢复。"
                          okText="删除"
                          okButtonProps={{ danger: true }}
                          cancelText="取消"
                          onConfirm={() => deleteMutation.mutate(conv.id)}
                        >
                          <Button
                            type="text"
                            size="small"
                            danger
                            icon={<DeleteOutlined />}
                            aria-label="删除会话"
                          />
                        </Popconfirm>
                      </Space>
                    </div>
                    <div style={{ display: "flex", justifyContent: "space-between" }}>
                      <Typography.Text
                        type="secondary"
                        style={{ fontSize: 11 }}
                      >
                        {conv.message_count} 条
                      </Typography.Text>
                      {conv.status !== "active" && (
                        <Typography.Text
                          type="secondary"
                          style={{ fontSize: 11 }}
                        >
                          已关闭
                        </Typography.Text>
                      )}
                    </div>
                  </div>
                </List.Item>
              )}
            />
          </div>
        ))}
        {filtered.length === 0 && (
          <div
            style={{
              padding: 24,
              textAlign: "center",
              color: "rgba(128,128,128,0.4)",
              fontSize: 13,
            }}
          >
            暂无会话
          </div>
        )}
      </div>

      {/* 重命名弹窗 */}
      <Modal
        title="重命名会话"
        open={!!renaming}
        onCancel={() => setRenaming(null)}
        onOk={() =>
          renaming && renaming.title.trim() && renameMutation.mutate(renaming)
        }
        confirmLoading={renameMutation.isPending}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <Input
          value={renaming?.title ?? ""}
          maxLength={255}
          onChange={(e) =>
            setRenaming((prev) =>
              prev ? { ...prev, title: e.target.value } : prev,
            )
          }
          onPressEnter={() =>
            renaming && renaming.title.trim() && renameMutation.mutate(renaming)
          }
          placeholder="会话名称"
        />
      </Modal>
    </div>
  );
}
