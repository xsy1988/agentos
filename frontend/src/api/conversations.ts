/** 对话 API */
import { api } from "./client";
import type { ConversationOut, MessageOut, SendMessageOut } from "./types";

export const conversationsApi = {
  list: () => api.get<ConversationOut[]>("/conversations"),
  create: (title: string, agentId?: string) =>
    api.post<ConversationOut>("/conversations", { title, agent_id: agentId }),
  messages: (convId: string) =>
    api.get<MessageOut[]>(`/conversations/${convId}/messages`),
  sendMessage: (convId: string, text: string) =>
    api.post<SendMessageOut>(`/conversations/${convId}/messages`, { text }),
};
