/** 对话 API */
import { api } from "./client";
import type { ConversationOut, MessageOut, SendMessageOut } from "./types";

/** 发消息的可选参数（run 级覆盖 / 附件 / 涉密确认 / 幂等键），InputBar 与对话页共用。 */
export interface SendMessageOptions {
  modelProviderId?: string;
  attachmentIds?: string[];
  confirmUpload?: boolean;
  forceCurrentTask?: boolean;
  clientMessageId?: string;
}

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
  // clientMessageId：前端生成的幂等键（P1-9），重试复用同一个键 → 后端返回首次的 run
  sendMessage: (
    convId: string,
    text: string,
    opts: SendMessageOptions = {},
  ) =>
    api.post<SendMessageOut>(`/conversations/${convId}/messages`, {
      text,
      model_provider_id: opts.modelProviderId ?? null,
      attachment_ids: opts.attachmentIds ?? [],
      confirm_upload: opts.confirmUpload ?? false,
      // 「仍在本会话继续」：跳过新主任务检测，直接在当前任务里执行（ADR-27）
      force_current_task: opts.forceCurrentTask ?? false,
      client_message_id: opts.clientMessageId ?? null,
    }),
};
