/** 能力体系 API */
import { api } from "./client";
import type { CapabilityOut, CapabilityToolOut } from "./types";

// 与后端 CapabilitySmokeReport.checks 对齐（smoke.py 逐条 {name, ok, detail}）
export interface SmokeCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface CapabilityCreatedOut {
  capability: CapabilityOut;
  smoke: { passed: boolean; checks: SmokeCheck[]; summary: string };
}

export const capabilitiesApi = {
  list: (includeDisabled = false) =>
    api.get<CapabilityOut[]>("/capabilities", { include_disabled: includeDisabled }),
  get: (id: string) => api.get<CapabilityOut>(`/capabilities/${id}`),
  create: (body: Record<string, unknown>) =>
    api.post<CapabilityCreatedOut>("/capabilities", body),
  update: (id: string, body: Record<string, unknown>) =>
    api.patch<CapabilityOut>(`/capabilities/${id}`, body),
  del: (id: string) => api.del(`/capabilities/${id}`),
  tools: (id: string) => api.get<CapabilityToolOut[]>(`/capabilities/${id}/tools`),
  setTool: (id: string, toolName: string, enabled: boolean) =>
    api.patch<CapabilityToolOut>(`/capabilities/${id}/tools/${toolName}?enabled=${enabled}`),
  // Agent 绑定
  bindings: (agentId: string) =>
    api.get<unknown[]>(`/agents/${agentId}/bindings`),
  upsertBinding: (agentId: string, capabilityId: string, mode: string) =>
    api.post(`/agents/${agentId}/bindings`, { capability_id: capabilityId, mode }),
  delBinding: (agentId: string, capabilityId: string) =>
    api.del(`/agents/${agentId}/bindings/${capabilityId}`),
};
