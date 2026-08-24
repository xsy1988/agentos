/** 知识库 API */
import { api } from "./client";
import type { ChunkOut, DocDetailOut, DocOut, FolderOut, SearchHit } from "./types";

export const knowledgeApi = {
  folders: () => api.get<FolderOut[]>("/kb/folders"),
  createFolder: (body: { name: string; parent_id?: string; description?: string }) =>
    api.post<FolderOut>("/kb/folders", body),
  updateFolder: (id: string, body: Record<string, unknown>) =>
    api.patch<FolderOut>(`/kb/folders/${id}`, body),
  delFolder: (id: string) => api.del(`/kb/folders/${id}`),
  docs: (folderId?: string) =>
    api.get<DocOut[]>("/kb/docs", { folder_id: folderId }),
  docDetail: (id: string) => api.get<DocDetailOut>(`/kb/docs/${id}`),
  parsePreview: (id: string) => api.get<{ markdown: string }>(`/kb/docs/${id}/parse`),
  chunks: (id: string, offset = 0, limit = 50) =>
    api.get<ChunkOut[]>(`/kb/docs/${id}/chunks`, { offset, limit }),
  updateChunk: (id: string, content: string) =>
    api.patch<ChunkOut>(`/kb/chunks/${id}`, { content }),
  delChunk: (id: string) => api.del(`/kb/chunks/${id}`),
  rechunk: (id: string, body: { target_tokens?: number; overlap_tokens?: number }) =>
    api.post<DocOut>(`/kb/docs/${id}/rechunk`, body),
  reembed: (id: string) => api.post<DocOut>(`/kb/docs/${id}/reembed`, {}),
  createDoc: (body: { file_id: string; folder_id: string; title: string }) =>
    api.post<DocOut>("/kb/docs", body),
  retryDoc: (id: string) => api.post<DocOut>(`/kb/docs/${id}/retry`),
  delDoc: (id: string) => api.del(`/kb/docs/${id}`),
  search: (body: { query: string; folders?: string[]; k?: number }) =>
    api.post<SearchHit[]>("/kb/search", body),
  reindex: () => api.post<{ scheduled: number }>("/kb/reindex"),
  parserHealth: () => api.get<{ healthy: boolean }>("/kb/parser-health"),
};
