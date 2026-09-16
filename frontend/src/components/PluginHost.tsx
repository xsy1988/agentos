/**
 * plugin 前端宿主（决策1，§3.5）：侧边栏是 plugin 前端的唯一宿主。
 *
 * 两模式：
 * - iframe：加载 plugin 自带 Web 页，postMessage 桥接
 *   （iframe→平台：READY/RESIZE/SUBMIT/CANCEL/CALL_MCP；平台→iframe：WORKER_CONTEXT）；
 * - server_driven：平台用 AntD 原生渲染 JSON UI schema（零自定义 JS、只渲染白名单组件，最轻）。
 *
 * 统一结构化回传契约（两模式一致）：{action:"submit"|"cancel", data, applied?}
 * → runsApi.confirm(runId, action, {data, applied}) → 写入子任务 resolution → 引擎续跑。
 *
 * 安全：iframe sandbox 最小授权 + src origin 允许名单校验；server_driven 不执行 plugin JS。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  InputNumber,
  Space,
  Spin,
  Table,
  Typography,
  message,
} from "antd";
import { CloseOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { capabilitiesApi } from "@/api/capabilities";
import { runsApi } from "@/api/runs";
import { useUIStore } from "@/store/ui";
import type { FrontendManifest, SidebarDescriptor, SidebarResult } from "@/api/types";

// ---- server_driven JSON UI schema（白名单组件，平台原生渲染）----
interface SDColumn {
  key: string;
  title: string;
  editable?: boolean;
  width?: number;
}
interface SDAction {
  key: string;
  label: string;
  kind?: "submit" | "cancel";
  style?: "primary" | "default" | "danger";
}
interface SDSchema {
  title?: string;
  description?: string;
  table?: {
    columns?: SDColumn[];
    rows?: Record<string, unknown>[];
    selectable?: boolean;
    deletable?: boolean;
  };
  fields?: { name: string; label: string; type?: "text" | "textarea" | "number" }[];
  actions?: SDAction[];
}

interface Row extends Record<string, unknown> {
  _rid: number;
}

function Header({ title, onClose }: { title: string; onClose: () => void }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        padding: "10px 12px",
        borderBottom: "1px solid var(--ant-color-border-secondary)",
        position: "sticky",
        top: 0,
        background: "var(--ant-color-bg-container)",
        zIndex: 2,
      }}
    >
      <Typography.Text strong style={{ fontSize: 13 }}>
        {title}
      </Typography.Text>
      <Button
        type="text"
        size="small"
        icon={<CloseOutlined />}
        onClick={onClose}
        aria-label="关闭侧边栏"
      />
    </div>
  );
}

/** server_driven：平台用 AntD 渲染 JSON UI schema（选/删/改一张表 + 附加字段）。 */
function ServerDrivenView({
  schema,
  initData,
  onSubmit,
}: {
  schema: SDSchema;
  initData: Record<string, unknown>;
  onSubmit: (r: SidebarResult) => void;
}) {
  const seedRows = useMemo<Row[]>(() => {
    const raw = (schema.table?.rows ?? initData.rows ?? []) as Record<string, unknown>[];
    return raw.map((r, i) => ({ ...r, _rid: i }));
  }, [schema.table?.rows, initData.rows]);

  const [rows, setRows] = useState<Row[]>(seedRows);
  const [selected, setSelected] = useState<number[]>([]);
  const [fields, setFields] = useState<Record<string, unknown>>(() => {
    const f: Record<string, unknown> = {};
    for (const item of schema.fields ?? []) f[item.name] = initData[item.name] ?? "";
    return f;
  });
  useEffect(() => setRows(seedRows), [seedRows]);

  const columns = schema.table?.columns ?? [];
  const selectable = schema.table?.selectable !== false && columns.length > 0;
  const deletable = schema.table?.deletable === true;

  const editCell = (rid: number, key: string, value: unknown) =>
    setRows((prev) => prev.map((r) => (r._rid === rid ? { ...r, [key]: value } : r)));

  const tableColumns = [
    ...columns.map((c) => ({
      title: c.title,
      dataIndex: c.key,
      key: c.key,
      width: c.width,
      render: (_: unknown, row: Row) =>
        c.editable ? (
          <Input
            size="small"
            value={String(row[c.key] ?? "")}
            onChange={(e) => editCell(row._rid, c.key, e.target.value)}
          />
        ) : (
          <span style={{ fontSize: 12 }}>{String(row[c.key] ?? "")}</span>
        ),
    })),
    ...(deletable
      ? [
          {
            title: "",
            key: "_del",
            width: 48,
            render: (_: unknown, row: Row) => (
              <Button
                size="small"
                type="text"
                danger
                onClick={() => {
                  setRows((prev) => prev.filter((r) => r._rid !== row._rid));
                  setSelected((prev) => prev.filter((k) => k !== row._rid));
                }}
              >
                删
              </Button>
            ),
          },
        ]
      : []),
  ];

  const actions: SDAction[] =
    schema.actions && schema.actions.length > 0
      ? schema.actions
      : [
          { key: "submit", label: "确认", kind: "submit", style: "primary" },
          { key: "cancel", label: "取消", kind: "cancel" },
        ];

  const doSubmit = (action: SDAction) => {
    if (action.kind === "cancel") {
      onSubmit({ action: "cancel" });
      return;
    }
    const cleanRows = rows.map(({ _rid, ...rest }) => rest);
    onSubmit({
      action: "submit",
      data: { rows: cleanRows, selected, fields },
    });
  };

  return (
    <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 12 }}>
      {schema.description && (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
          {schema.description}
        </Typography.Paragraph>
      )}
      {columns.length > 0 && (
        <Table<Row>
          size="small"
          rowKey="_rid"
          dataSource={rows}
          columns={tableColumns}
          pagination={false}
          scroll={{ y: 320 }}
          rowSelection={
            selectable
              ? {
                  selectedRowKeys: selected,
                  onChange: (keys) => setSelected(keys as number[]),
                }
              : undefined
          }
        />
      )}
      {(schema.fields ?? []).map((f) => (
        <div key={f.name}>
          <Typography.Text style={{ fontSize: 12 }}>{f.label}</Typography.Text>
          {f.type === "textarea" ? (
            <Input.TextArea
              rows={2}
              value={String(fields[f.name] ?? "")}
              onChange={(e) => setFields({ ...fields, [f.name]: e.target.value })}
            />
          ) : f.type === "number" ? (
            <InputNumber
              style={{ width: "100%" }}
              value={fields[f.name] as number}
              onChange={(v) => setFields({ ...fields, [f.name]: v })}
            />
          ) : (
            <Input
              value={String(fields[f.name] ?? "")}
              onChange={(e) => setFields({ ...fields, [f.name]: e.target.value })}
            />
          )}
        </div>
      ))}
      <Space style={{ justifyContent: "flex-end", width: "100%" }}>
        {actions.map((a) => (
          <Button
            key={a.key}
            type={a.style === "primary" ? "primary" : "default"}
            danger={a.style === "danger"}
            onClick={() => doSubmit(a)}
          >
            {a.label}
          </Button>
        ))}
      </Space>
    </div>
  );
}

/** iframe：加载 plugin 自带 Web 页，postMessage 桥接（握手 + 结构化回传）。 */
function IframeView({
  manifest,
  descriptor,
  onSubmit,
}: {
  manifest: FrontendManifest;
  descriptor: SidebarDescriptor;
  onSubmit: (r: SidebarResult) => void;
}) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const setWidth = useUIStore((s) => s.setContextPanelWidth);
  const submitRef = useRef(onSubmit);
  submitRef.current = onSubmit;

  // 期望 origin：allowlist_origin 优先，否则取 url 的 origin（postMessage 目标 + 消息来源校验）
  const expectedOrigin = useMemo(() => {
    if (manifest.allowlist_origin) return manifest.allowlist_origin;
    try {
      return manifest.url ? new URL(manifest.url).origin : null;
    } catch {
      return null;
    }
  }, [manifest.allowlist_origin, manifest.url]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      // 安全：只接受允许名单内 origin 的消息
      if (expectedOrigin && event.origin !== expectedOrigin) return;
      const msg = event.data as Record<string, unknown> | null;
      if (!msg || typeof msg !== "object") return;
      const post = (payload: Record<string, unknown>) =>
        iframeRef.current?.contentWindow?.postMessage(payload, expectedOrigin ?? "*");
      switch (msg.type) {
        case "READY":
          // 握手：iframe 就绪 → 平台下发 WORKER_CONTEXT（初始化数据/主题/语言）
          post({
            type: "WORKER_CONTEXT",
            task_id: descriptor.init_data?.task_id ?? null,
            step_id: descriptor.step_id ?? null,
            idempotency_key: descriptor.idempotency_key ?? null,
            data: descriptor.init_data ?? {},
            theme: "light", // 平台统一浅色主题，不再切换
            lang: "zh-CN",
          });
          break;
        case "RESIZE": {
          const w = Number(msg.width);
          if (Number.isFinite(w) && w > 0) setWidth(w);
          break;
        }
        case "SUBMIT":
          submitRef.current({
            action: "submit",
            data: msg.data,
            applied: msg.applied as SidebarResult["applied"],
          });
          break;
        case "CANCEL":
          submitRef.current({ action: "cancel" });
          break;
        case "CALL_MCP":
          // 可选代理路径未启用：提示 iframe 直接调写入类接口并把结果并入 applied 回传
          post({
            type: "CALL_MCP_RESULT",
            ok: false,
            error: "平台代理调用未启用：请在页面内直接调用写入类接口，并把结果并入 SUBMIT.applied 回传",
          });
          break;
        default:
          break;
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [expectedOrigin, descriptor, setWidth]);

  if (!manifest.url) {
    return (
      <Alert
        type="error"
        showIcon
        style={{ margin: 12 }}
        message="iframe 模式缺少 frontend.url"
      />
    );
  }
  return (
    <iframe
      ref={iframeRef}
      src={manifest.url}
      title={descriptor.title ?? "plugin 前端"}
      sandbox={manifest.sandbox ?? "allow-scripts allow-forms allow-same-origin allow-popups"}
      style={{ width: "100%", height: "100%", border: "none", display: "block" }}
    />
  );
}

export default function PluginHost({
  descriptor,
  fallbackRunId,
  onClose,
}: {
  descriptor: SidebarDescriptor;
  fallbackRunId: string | null;
  onClose: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);

  // 未内联 frontend 时按 plugin_capability_id 查能力清单
  const { data: cap, isLoading } = useQuery({
    queryKey: ["capability", descriptor.plugin_capability_id],
    queryFn: () => capabilitiesApi.get(descriptor.plugin_capability_id!),
    enabled: !descriptor.frontend && !!descriptor.plugin_capability_id,
  });

  const manifest: FrontendManifest | null =
    descriptor.frontend ?? ((cap?.payload?.frontend as FrontendManifest) || null);

  const submit = async (result: SidebarResult) => {
    const runId = descriptor.run_id ?? fallbackRunId;
    if (!runId) {
      message.warning("没有可回传的运行中任务，已关闭侧边栏");
      onClose();
      return;
    }
    setSubmitting(true);
    try {
      await runsApi.confirm(runId, result.action, {
        data: result.data,
        applied: result.applied,
      });
      message.success(result.action === "submit" ? "已提交，任务继续" : "已取消该决策");
      onClose();
    } catch (e) {
      message.error((e as Error).message || "回传失败");
    } finally {
      setSubmitting(false);
    }
  };

  const title = descriptor.title ?? "交互决策";

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <Header title={title} onClose={onClose} />
      <div style={{ flex: 1, minHeight: 0, overflow: "auto", position: "relative" }}>
        {submitting && (
          <Spin style={{ position: "absolute", inset: 0, margin: "auto", zIndex: 3 }} />
        )}
        {!manifest ? (
          isLoading ? (
            <Spin style={{ display: "block", margin: "24px auto" }} />
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="未找到 plugin 前端清单（payload.frontend）"
              style={{ marginTop: 24 }}
            />
          )
        ) : manifest.mode === "iframe" ? (
          <IframeView manifest={manifest} descriptor={descriptor} onSubmit={submit} />
        ) : (
          <ServerDrivenView
            schema={(manifest.schema ?? {}) as SDSchema}
            initData={descriptor.init_data ?? {}}
            onSubmit={submit}
          />
        )}
      </div>
    </div>
  );
}
