/** 通知 API */
import { api } from "./client";
import type { NotificationOut } from "./types";

export const notificationsApi = {
  list: (unreadOnly = false, limit = 50) =>
    api.get<NotificationOut[]>("/notifications", { unread_only: unreadOnly, limit }),
  markRead: (id: string) => api.post(`/notifications/${id}/read`),
  markAllRead: () => api.post("/notifications/read-all"),
};
