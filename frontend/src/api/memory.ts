/** 记忆 API */
import { api } from "./client";
import type { MemoryOut } from "./types";

export interface MemoryUpdateIn {
  title?: string;
  content?: string;
}

export const memoryApi = {
  list: (kind?: string) =>
    api.get<MemoryOut[]>("/memory", { kind }),
  update: (id: string, body: MemoryUpdateIn) =>
    api.patch<MemoryOut>(`/memory/${id}`, body),
  del: (id: string) => api.del(`/memory/${id}`),
  consolidate: (target?: string) =>
    api.post("/memory/consolidate", { target }),
};
