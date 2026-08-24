/** 文件上传 API */
import { api } from "./client";
import type { FileOut } from "./types";

export const filesApi = {
  upload: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return api.upload<FileOut>("/files/upload", formData);
  },
  /** 附件内容（blob，带鉴权；<img> 标签带不了 Authorization，用 objectURL 渲染） */
  fetchContent: async (fileId: string): Promise<Blob> => {
    const res = await api.raw(`/files/${fileId}/content`);
    if (!res.ok) throw new Error(`附件加载失败 (${res.status})`);
    return res.blob();
  },
};
