/**
 * Worker 文件包 API（/workers）：data/workers/ 目录的在线管理。
 * 管理本质 = 管理 WORKER.md 文件包：版本手动构建，编辑文件不产生新版本。
 */
import { api } from "./client";
import type {
  CapabilityRefsOut,
  FileNodeOut,
  SubWorkerOut,
  VersionBuildOut,
  WorkerFileOut,
  WorkerOut,
} from "./types";

export const workersApi = {
  /** Worker 列表（名称/描述/版本/启停状态） */
  list: () => api.get<WorkerOut[]>("/workers"),
  get: (name: string) => api.get<WorkerOut>(`/workers/${encodeURIComponent(name)}`),
  /** 新建 Worker 脚手架（v1 + manifest） */
  create: (body: { name: string; description?: string; icon?: string | null; color?: string | null }) =>
    api.post<WorkerOut>("/workers", body),
  /** 启停 / 切换 active 版本 */
  patch: (name: string, body: { enabled?: boolean; active_version?: string }) =>
    api.patch<WorkerOut>(`/workers/${encodeURIComponent(name)}`, body),
  del: (name: string) => api.del<void>(`/workers/${encodeURIComponent(name)}`),

  /** 手动构建新版本：复制当前生效版本 → vN+1，并把 active 指向新版本 */
  buildVersion: (name: string) =>
    api.post<VersionBuildOut>(`/workers/${encodeURIComponent(name)}/versions`),
  deleteVersion: (name: string, version: string) =>
    api.del<void>(`/workers/${encodeURIComponent(name)}/versions/${version}`),

  /** 版本内文件树（version 省略 = 生效版本） */
  tree: (name: string, version?: string) =>
    api.get<FileNodeOut>(`/workers/${encodeURIComponent(name)}/tree`, { version }),
  /** 读文件（历史版本只读展示） */
  getFile: (name: string, path: string, version?: string) =>
    api.get<WorkerFileOut>(`/workers/${encodeURIComponent(name)}/file`, { path, version }),
  /** 保存文件（只有 active 版本可写；不产生新版本） */
  putFile: (name: string, path: string, content: string, version?: string) =>
    api.put<WorkerFileOut>(
      `/workers/${encodeURIComponent(name)}/file`,
      { content },
      { path, version },
    ),
  /** 新建文件（references/*.md 等文本文件） */
  createFile: (name: string, body: { path: string; content?: string }, version?: string) =>
    api.post<WorkerFileOut>(`/workers/${encodeURIComponent(name)}/file`, body, { version }),
  deleteFile: (name: string, path: string, version?: string) =>
    api.del<void>(
      `/workers/${encodeURIComponent(name)}/file?path=${encodeURIComponent(path)}${version ? `&version=${version}` : ""}`,
    ),

  /** 新建子任务文件夹脚手架（sub_workers/<名>/WORKER.md） */
  createSubWorker: (
    name: string,
    body: { name: string; kind?: "main" | "branch"; description?: string },
    version?: string,
  ) =>
    api.post<SubWorkerOut>(`/workers/${encodeURIComponent(name)}/sub-workers`, body, { version }),

  /** 工具引用清单校验：WORKER.md capabilities ↔ 平台已注册能力（命中/缺失） */
  capabilityRefs: (name: string, version?: string) =>
    api.get<CapabilityRefsOut>(`/workers/${encodeURIComponent(name)}/capabilities`, { version }),
};
