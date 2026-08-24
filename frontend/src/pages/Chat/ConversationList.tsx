/**
 * 会话列表（前端设计 §3.1 左栏）。
 * 按日期分组、新建会话。
 */
import { Button, List, Typography, Input } from "antd";
import { PlusOutlined, SearchOutlined } from "@ant-design/icons";
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
}: {
  activeId: string | null;
  onSelect: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");

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

  const filtered = useMemo(() => {
    if (!search) return conversations;
    return conversations.filter((c) =>
      c.title.toLowerCase().includes(search.toLowerCase()),
    );
  }, [conversations, search]);

  // 分组
  const groups = useMemo(() => {
    const map = new Map<string, ConversationOut[]>();
    for (const c of filtered) {
      const key = formatDateGroup(c.last_message_at);
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
                    <Typography.Text
                      ellipsis
                      style={{ fontSize: 13 }}
                      strong={conv.id === activeId}
                    >
                      {conv.title}
                    </Typography.Text>
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
    </div>
  );
}
