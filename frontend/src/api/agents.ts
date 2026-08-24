/** Agents API */
import { api } from "./client";
import type { AgentOut } from "./types";

export const agentsApi = {
  list: (includeDisabled = false) =>
    api.get<AgentOut[]>("/agents", { include_disabled: includeDisabled }),
  get: (id: string) => api.get<AgentOut>(`/agents/${id}`),
  create: (body: Record<string, unknown>) =>
    api.post<AgentOut>("/agents", body),
  update: (id: string, body: Record<string, unknown>) =>
    api.patch<AgentOut>(`/agents/${id}`, body),
  del: (id: string) => api.del(`/agents/${id}`),
};
