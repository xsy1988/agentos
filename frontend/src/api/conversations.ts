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
  messages: (convId: string) =>
    api.get<MessageOut[]>(`/conversations/${convId}/messages`),
  sendMessage: (
    convId: string,
    text: string,
    modelProviderId?: string,
    attachmentIds?: string[],
    confirmUpload?: boolean,
  ) =>
    api.post<SendMessageOut>(`/conversations/${convId}/messages`, {
      text,
      model_provider_id: modelProviderId ?? null,
      attachment_ids: attachmentIds ?? [],
      confirm_upload: confirmUpload ?? false,
    }),
};
