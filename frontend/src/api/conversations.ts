/** 对话 API */
import { api } from "./client";
import type { ConversationOut, MessageOut, SendMessageOut } from "./types";

export const conversationsApi = {
  list: () => api.get<ConversationOut[]>("/conversations"),
  create: (title: string, agentId?: string) =>
    api.post<ConversationOut>("/conversations", { title, agent_id: agentId }),
  // 重命名 / 关闭（部分更新）
  update: (convId: string, body: { title?: string; status?: string }) =>
    api.patch<ConversationOut>(`/conversations/${convId}`, body),
  // 删除：消息/任务/事件/计划级联清除
  del: (convId: string) => api.del<void>(`/conversations/${convId}`),
  // beforeId 游标翻页（加载更早）；默认返回最近 limit 条（升序）
  messages: (convId: string, beforeId?: string) =>
    api.get<MessageOut[]>(
      `/conversations/${convId}/messages`,
      beforeId ? { before_id: beforeId } : undefined,
    ),
  sendMessage: (
    convId: string,
    text: string,
    modelProviderId?: string,
    attachmentIds?: string[],
    confirmUpload?: boolean,
    forceCurrentTask?: boolean,
  ) =>
    api.post<SendMessageOut>(`/conversations/${convId}/messages`, {
      text,
      model_provider_id: modelProviderId ?? null,
      attachment_ids: attachmentIds ?? [],
      confirm_upload: confirmUpload ?? false,
      // 「仍在本会话继续」：跳过新主任务检测，直接在当前任务里执行（ADR-27）
      force_current_task: forceCurrentTask ?? false,
    }),
};
