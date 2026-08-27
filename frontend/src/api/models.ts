/** 模型接入 API */
import { api } from "./client";
import type { ModelProviderOut } from "./types";

export const modelsApi = {
  list: (kind?: string) =>
    api.get<ModelProviderOut[]>("/models", { kind }),
  create: (body: Record<string, unknown>) =>
    api.post<ModelProviderOut>("/models", body),
  update: (id: string, body: Record<string, unknown>) =>
    api.patch<ModelProviderOut>(`/models/${id}`, body),
  test: (id: string) => api.post(`/models/${id}/test`),
  syncGateway: () =>
    api.post<{ gateway: string; total: number; migrated: number; created: number }>(
      "/models/sync-gateway",
    ),
};
