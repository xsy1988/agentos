/** 文件上传 API */
import { api } from "./client";
import type { FileOut } from "./types";

export const filesApi = {
  upload: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return api.upload<FileOut>("/files/upload", formData);
  },
};
