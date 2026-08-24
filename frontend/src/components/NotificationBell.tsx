/**
 * 通知中心（前端设计 §4）：顶栏铃铛下拉，分级排序。
 */
import { Popover, Badge, List, Button, Tag, Empty, Typography } from "antd";
import { BellOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { NotificationOut } from "@/api/types";

const KIND_LABEL: Record<string, string> = {
  run_done: "完成",
  run_failed: "失败",
  alarm: "提醒",
  system: "系统",
};
const KIND_COLOR: Record<string, string> = {
  run_failed: "red",
  run_done: "green",
  alarm: "blue",
  system: "default",
};

export default function NotificationBell() {
  const queryClient = useQueryClient();
  const { data: notifications = [] } = useQuery({
    queryKey: ["notifications", "unread"],
    queryFn: () =>
      api.get<NotificationOut[]>("/notifications", { unread_only: true }),
    refetchInterval: 60_000,
  });

  const unreadCount = notifications.length;

  const handleMarkRead = async (id: string) => {
    await api.post(`/notifications/${id}/read`);
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  const handleMarkAll = async () => {
    await api.post("/notifications/read-all");
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  const content = (
    <div style={{ width: 360 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "4px 8px",
          borderBottom: "1px solid rgba(128,128,128,0.2)",
        }}
      >
        <Typography.Text strong>通知中心</Typography.Text>
        {unreadCount > 0 && (
          <Button type="link" size="small" onClick={handleMarkAll}>
            全部已读
          </Button>
        )}
      </div>
      {notifications.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无未读通知"
          style={{ margin: "12px 0" }}
        />
      ) : (
        <List
          size="small"
          dataSource={notifications.slice(0, 20)}
          renderItem={(n) => (
            <List.Item
              style={{ cursor: "pointer", padding: "8px 12px" }}
              onClick={() => handleMarkRead(n.id)}
            >
              <div style={{ width: "100%" }}>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    marginBottom: 2,
                  }}
                >
                  <Tag color={KIND_COLOR[n.kind] ?? "default"}>
                    {KIND_LABEL[n.kind] ?? n.kind}
                  </Tag>
                  <Typography.Text style={{ fontSize: 12 }} type="secondary">
                    {new Date(n.created_at).toLocaleString("zh-CN")}
                  </Typography.Text>
                </div>
                <Typography.Text style={{ fontSize: 13 }} strong>
                  {n.title}
                </Typography.Text>
                <div>
                  <Typography.Text style={{ fontSize: 12 }} type="secondary">
                    {n.content}
                  </Typography.Text>
                </div>
              </div>
            </List.Item>
          )}
        />
      )}
    </div>
  );

  return (
    <Popover content={content} trigger="click" placement="bottomRight">
      <Badge count={unreadCount} size="small" offset={[-2, 2]}>
        <BellOutlined style={{ fontSize: 18, cursor: "pointer" }} />
      </Badge>
    </Popover>
  );
}
