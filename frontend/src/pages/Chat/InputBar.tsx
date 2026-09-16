/**
 * 消息输入栏（前端设计 §3.1 底部）。
 * Markdown 编辑模式、发送、终止进行中的 run。
 * 模型选择：run 级覆盖（随消息快照固化），默认跟随 Agent 绑定模型。
 * 附件：选择/粘贴/拖入 → 先传 /files/upload 拿 file_id → 发消息只传 id 引用。
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Input, Button, Space, Select, Tooltip, Modal, message as antdMessage } from "antd";
import {
  SendOutlined,
  StopOutlined,
  PaperClipOutlined,
  FileTextOutlined,
  DeleteOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { runsApi } from "@/api/runs";
import { modelsApi } from "@/api/models";
import { filesApi } from "@/api/files";
import { ApiError } from "@/api/client";
import type { ModelProviderOut } from "@/api/types";

const MODEL_PREF_KEY = "chat.modelProviderId";

// 附件限制（与后端 files/service.py 对齐）
const MAX_ATTACHMENTS = 5;
const IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".webp", ".gif"];
const DOC_EXTS = [".pdf", ".docx", ".md", ".txt", ".xlsx"];
const MAX_IMAGE_SIZE = 5 * 1024 * 1024;
const MAX_DOC_SIZE = 10 * 1024 * 1024;

interface DraftAttachment {
  key: string;
  filename: string;
  size: number;
  isImage: boolean;
  previewUrl?: string; // 图片本地预览（objectURL）
  fileId?: string; // 上传完成才有
  status: "uploading" | "done" | "error";
}

function checkFile(file: File): string | null {
  const name = file.name.toLowerCase();
  const isImage = IMAGE_EXTS.some((e) => name.endsWith(e));
  const isDoc = DOC_EXTS.some((e) => name.endsWith(e));
  if (!isImage && !isDoc) return `不支持的类型：${file.name}`;
  const limit = isImage ? MAX_IMAGE_SIZE : MAX_DOC_SIZE;
  if (file.size > limit) {
    return `${file.name} 超出大小限制（${limit / 1024 / 1024}MB）`;
  }
  return null;
}

export default function InputBar({
  onSend,
  disabled,
  runId,
}: {
  onSend: (
    text: string,
    modelProviderId?: string,
    attachmentIds?: string[],
    confirmUpload?: boolean,
  ) => Promise<void>;
  disabled: boolean;
  runId?: string;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [attachments, setAttachments] = useState<DraftAttachment[]>([]);
  // 拖拽高亮：文件拖入时输入容器边框点亮
  const [dragActive, setDragActive] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // 选择持久化：切换会话/刷新后保留（localStorage）
  const [modelId, setModelId] = useState<string | undefined>(() =>
    localStorage.getItem(MODEL_PREF_KEY) || undefined,
  );

  useEffect(() => {
    if (modelId) localStorage.setItem(MODEL_PREF_KEY, modelId);
    else localStorage.removeItem(MODEL_PREF_KEY);
  }, [modelId]);

  const { data: models } = useQuery({
    queryKey: ["models", "llm"],
    queryFn: () => modelsApi.list("llm"),
    staleTime: 60_000,
  });
  const llms = (models ?? []).filter((m: ModelProviderOut) => m.status === "enabled");

  // 已选模型被删除/停用时自动回退「跟随 Agent」
  useEffect(() => {
    if (modelId && !llms.some((m) => m.id === modelId)) setModelId(undefined);
  }, [llms, modelId]);

  const uploading = attachments.some((a) => a.status === "uploading");

  const addFiles = (files: FileList | File[]) => {
    const room = MAX_ATTACHMENTS - attachments.length;
    if (room <= 0) {
      antdMessage.warning(`最多 ${MAX_ATTACHMENTS} 个附件`);
      return;
    }
    const list = Array.from(files).slice(0, room);
    for (const f of list) {
      const err = checkFile(f);
      if (err) {
        antdMessage.error(err);
        continue;
      }
      const key = `${f.name}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const isImage = f.type.startsWith("image/");
      const draft: DraftAttachment = {
        key,
        filename: f.name,
        size: f.size,
        isImage,
        previewUrl: isImage ? URL.createObjectURL(f) : undefined,
        status: "uploading",
      };
      setAttachments((prev) => [...prev, draft]);
      filesApi
        .upload(f)
        .then((out) => {
          setAttachments((prev) =>
            prev.map((a) => (a.key === key ? { ...a, fileId: out.id, status: "done" } : a)),
          );
        })
        .catch((e: Error) => {
          antdMessage.error(`上传失败：${e.message}`);
          setAttachments((prev) => prev.filter((a) => a.key !== key));
          if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
        });
    }
  };

  const removeAttachment = (key: string) => {
    setAttachments((prev) => {
      const a = prev.find((x) => x.key === key);
      if (a?.previewUrl) URL.revokeObjectURL(a.previewUrl);
      return prev.filter((x) => x.key !== key);
    });
  };

  const clearAttachments = () => {
    attachments.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
    setAttachments([]);
  };

  const canSend =
    !disabled && !sending && !uploading && (text.trim().length > 0 || attachments.some((a) => a.status === "done"));

  const handleSend = async () => {
    if (!canSend) return;
    setSending(true);
    const ids = attachments.filter((a) => a.status === "done").map((a) => a.fileId!);
    const payload = [text.trim(), modelId, ids] as const;
    try {
      let confirmUpload = false;
      try {
        await onSend(...payload, confirmUpload);
      } catch (e) {
        // 图片外发确认门：带图且生效模型支持视觉 → 428，弹窗确认后携 confirm_upload 重发
        if (!(e instanceof ApiError) || e.status !== 428) throw e;
        const ok = await new Promise<boolean>((resolve) => {
          Modal.confirm({
            title: "图片外发确认",
            content: `${e.detail}。确认后图片将由模型服务商处理，请确保不包含涉密/敏感内容。`,
            okText: "不涉密，上传",
            okType: "primary",
            cancelText: "取消发送",
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!ok) return; // 取消：保留输入内容
        confirmUpload = true;
        await onSend(...payload, confirmUpload);
      }
      setText("");
      clearAttachments();
    } catch {
      // 发送失败：保留输入内容，可修改后重试
    } finally {
      setSending(false);
    }
  };

  const handleAbort = async () => {
    if (!runId) return;
    await runsApi.abort(runId);
  };

  return (
    <div
      style={{
        padding: "12px 16px 6px",
        borderTop: "1px solid var(--ant-color-border-secondary)",
      }}
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setDragActive(true);
      }}
      onDragLeave={() => setDragActive(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragActive(false);
        if (!disabled && e.dataTransfer.files.length > 0) addFiles(e.dataTransfer.files);
      }}
    >
      {/* 统一输入容器：附件区 + 输入框 + 底部工具栏（ChatGPT 式一体式布局） */}
      <div className={`input-shell${dragActive ? " drag-over" : ""}`}>
        {/* 附件 chip 列表（容器内顶部） */}
        {attachments.length > 0 && (
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: 6,
              padding: "8px 10px 0",
            }}
          >
            {attachments.map((a) => (
              <div
                key={a.key}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "4px 8px",
                  borderRadius: 6,
                  background: "var(--ant-color-fill-secondary)",
                  maxWidth: 280,
                }}
              >
                {a.isImage && a.previewUrl ? (
                  <img
                    src={a.previewUrl}
                    alt={a.filename}
                    style={{ width: 40, height: 40, objectFit: "cover", borderRadius: 4 }}
                  />
                ) : (
                  <FileTextOutlined />
                )}
                <span
                  style={{
                    fontSize: 12,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {a.filename}
                </span>
                {a.status === "uploading" ? (
                  <LoadingOutlined style={{ fontSize: 12 }} />
                ) : (
                  <DeleteOutlined
                    style={{ fontSize: 12, cursor: "pointer", color: "var(--ant-color-text-secondary)" }}
                    onClick={() => removeAttachment(a.key)}
                  />
                )}
              </div>
            ))}
          </div>
        )}

        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={[...IMAGE_EXTS, ...DOC_EXTS].join(",")}
          style={{ display: "none" }}
          onChange={(e) => {
            if (e.target.files?.length) addFiles(e.target.files);
            e.target.value = ""; // 同名文件可重复选择
          }}
        />

        {/* 输入框：无边框透明，融入容器 */}
        <Input.TextArea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onPaste={(e) => {
            const files = e.clipboardData?.files;
            if (files?.length) {
              e.preventDefault();
              addFiles(files);
            }
          }}
          placeholder="输入消息…"
          autoSize={{ minRows: 1, maxRows: 8 }}
          disabled={disabled}
          variant="borderless"
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          style={{
            resize: "none",
            padding: "8px 12px 4px",
            fontSize: 14,
            background: "transparent",
          }}
        />

        {/* 底部工具栏：左侧附件+模型，右侧发送/终止 */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "0 6px 6px 4px",
          }}
        >
          <Space size={0}>
            <Tooltip title="添加图片或文件（也可直接粘贴/拖入）">
              <Button
                type="text"
                icon={<PaperClipOutlined />}
                onClick={() => fileInputRef.current?.click()}
                disabled={disabled}
                style={{ color: "var(--ant-color-text-secondary)" }}
              />
            </Tooltip>
            {/* 模型选择器不加 Tooltip：切模型时弹出提示气泡反而打扰 */}
            <Select
              size="small"
              variant="borderless"
              className="model-select"
              style={{ minWidth: 160, maxWidth: 240 }}
              // 下拉宽度独立于触发器：触发器窄（弱化），下拉要能看全模型名
              popupMatchSelectWidth={320}
              value={modelId ?? "follow-agent"}
              onChange={(v: string) => setModelId(v === "follow-agent" ? undefined : v)}
              options={[
                { value: "follow-agent", label: "跟随 Agent 默认", model_name: "" },
                ...llms.map((m) => ({
                  value: m.id,
                  label: m.name,
                  model_name: m.model_name,
                })),
              ]}
              // 下拉项两行：名称 + model_name 小字辅助行（长名称不截断，模型标识可辨识）。
              // optionRender 回调参数是 FlattenOptionData 包装，自定义字段在 data 下。
              optionRender={(option) => {
                const { label, model_name } = (
                  option.data ?? option
                ) as {
                  label?: ReactNode;
                  model_name?: string;
                };
                return (
                  <div className="model-option">
                    <div className="model-option-name">{label}</div>
                    {model_name ? (
                      <div className="model-option-sub">{model_name}</div>
                    ) : null}
                  </div>
                );
              }}
            />
          </Space>

          {disabled && runId ? (
            <Tooltip title="终止当前任务">
              <Button danger shape="circle" icon={<StopOutlined />} onClick={handleAbort} />
            </Tooltip>
          ) : (
            <Tooltip title={canSend ? "发送（Enter）" : "输入内容后发送"}>
              <Button
                type="primary"
                shape="circle"
                icon={<SendOutlined />}
                onClick={handleSend}
                loading={sending}
                disabled={!canSend}
              />
            </Tooltip>
          )}
        </div>
      </div>

      {/* 快捷键提示：容器外一行小字，不抢输入区视觉 */}
      <div
        style={{
          textAlign: "center",
          fontSize: 11,
          color: "var(--ant-color-text-tertiary)",
          marginTop: 6,
        }}
      >
        Enter 发送 · Shift+Enter 换行 · 支持粘贴 / 拖入图片与文件
      </div>
    </div>
  );
}
