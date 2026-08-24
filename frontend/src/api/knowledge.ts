/** 知识库 API */
import { api } from "./client";
import type { FolderOut, DocOut, SearchHit } from "./types";

export const knowledgeApi = {
  folders: () => api.get<FolderOut[]>("/kb/folders"),
  createFolder: (body: { name: string; parent_id?: string; description?: string }) =>
    api.post<FolderOut>("/kb/folders", body),
  updateFolder: (id: string, body: Record<string, unknown>) =>
    api.patch<FolderOut>(`/kb/folders/${id}`, body),
  delFolder: (id: string) => api.del(`/kb/folders/${id}`),
  docs: (folderId?: string) =>
    api.get<DocOut[]>("/kb/docs", { folder_id: folderId }),
  createDoc: (body: { file_id: string; folder_id: string; title: string }) =>
    api.post<DocOut>("/kb/docs", body),
  retryDoc: (id: string) => api.post<DocOut>(`/kb/docs/${id}/retry`),
  delDoc: (id: string) => api.del(`/kb/docs/${id}`),
  search: (body: { query: string; folders?: string[]; k?: number }) =>
    api.post<SearchHit[]>("/kb/search", body),
  reindex: () => api.post<{ scheduled: number }>("/kb/reindex"),
  parserHealth: () => api.get<{ healthy: boolean }>("/kb/parser-health"),
};
