/** 结果产物 API（P0-5）：run 清单 + 单条取件。 */
import { api } from "./client";
import type { ArtifactDetailOut, ArtifactOut } from "./types";

export const artifactsApi = {
  /** run 的全部产物（按创建序），正文不在此响应里。 */
  listByRun: (runId: string) => api.get<ArtifactOut[]>(`/runs/${runId}/artifacts`),
  /** 单条产物：text/json 回正文 JSON；file 由后端重定向到 files 取件。 */
  get: (artifactId: string) => api.get<ArtifactDetailOut>(`/artifacts/${artifactId}`),
  /** file 产物的二进制取件：`<a download>` 带不了鉴权头，故走 fetch 再落盘。 */
  fetchContent: async (artifactId: string): Promise<Blob> => {
    const res = await api.raw(`/artifacts/${artifactId}`);
    if (!res.ok) throw new Error(`产物加载失败 (${res.status})`);
    return res.blob();
  },
};
