/**
 * 聊天卡片统一渲染器（决策2，§3.6）：注册表驱动，按 card_type 分派内置/自定义渲染器。
 *
 * 后端 confirmation_request 事件 payload 结构（runtime._pause_for_confirmation）保持
 * `{reason, payload}` 不变；reason 即 card_type，payload(inner) 是卡片主体：
 * - plan_review：{plan: [{seq, text, status}]}
 * - high_risk_tool：{calls: [{name, args}], risk_levels: {name: level}}
 * - subtask_clarification：{question, title, step_id, kind}（ADR-24，文本答复回注）
 * - interactive_decision：{card_type, title, summary, severity, body, actions[], sidebar, step_id,
 *     idempotency_key}（决策2 新增，带 sidebar；点「去处理」开侧边栏渲染 plugin 前端，§3.5）
 *
 * 自定义卡（业务扩展，不改内核）：未命中注册表的 card_type 落到 GenericCard——
 * 按声明式模板 {title, summary, severity, body, actions} 通用渲染（server_driven 思路）；
 * 需要富交互的卡面经 actions.kind=open_sidebar 走 plugin 前端（iframe），即「plugin 自带卡面」。
 */
import { useState } from "react";
import type { ComponentType, ReactNode } from "react";
import { Button, Card, Input, Space, Table, Tag, Typography } from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  ExclamationCircleOutlined,
  ExportOutlined,
  InfoCircleOutlined,
  InteractionOutlined,
  QuestionCircleOutlined,
} from "@ant-design/icons";
import { artifactsApi } from "@/api/artifacts";
import type { RunArtifactRef, SidebarDescriptor } from "@/api/types";
import { fmtSize } from "@/utils/format";

/** 卡片动作（§3.6）：kind 决定点击行为。 */
export interface CardAction {
  key: string;
  label: string;
  kind: "confirm" | "reject" | "open_sidebar" | "custom";
  style?: "primary" | "default" | "danger";
}

/** 传给每个卡片渲染器的上下文。 */
export interface CardContext {
  cardType: string;
  /** 事件 payload.payload：卡片主体 */
  inner: Record<string, unknown>;
  runId: string;
  /** 计划/高危工具：approved/rejected；支线提问：用户自由文本；决策卡：action.key */
  onConfirm: (answer?: string) => void;
  onReject: () => void;
  /** interactive_decision「去处理」：打开右侧 plugin 前端侧边栏（§3.5） */
  onOpenSidebar: (sidebar: SidebarDescriptor) => void;
}

type CardComponent = ComponentType<{ ctx: CardContext }>;

type Severity = "info" | "warn" | "danger";

const SEVERITY_META: Record<Severity, { color: string; bg: string }> = {
  info: { color: "var(--ant-color-primary)", bg: "rgba(22,119,255,0.06)" },
  warn: { color: "var(--ant-color-warning)", bg: "rgba(250,173,20,0.06)" },
  danger: { color: "var(--ant-color-error)", bg: "rgba(255,77,79,0.06)" },
};

function normSeverity(v: unknown, fallback: Severity = "info"): Severity {
  const s = String(v ?? "").toLowerCase();
  return s === "danger" || s === "warn" || s === "info" ? s : fallback;
}

/** 卡片外壳：统一非模态样式（出现在输入框上方），按 severity 着色。 */
function Shell({
  severity,
  title,
  icon,
  children,
}: {
  severity: Severity;
  title: string;
  icon?: ReactNode;
  children: ReactNode;
}) {
  const meta = SEVERITY_META[severity];
  return (
    <Card
      size="small"
      style={{
        margin: "0 16px 8px",
        borderColor: meta.color,
        borderWidth: severity === "danger" ? 2 : 1,
        background: meta.bg,
      }}
    >
      <Space size={6} style={{ marginBottom: 8 }}>
        {icon}
        <Typography.Text strong>{title}</Typography.Text>
      </Space>
      {children}
    </Card>
  );
}

interface PlanItem {
  seq: number;
  text: string;
  status: string;
}
interface RiskyCall {
  name: string;
  args: Record<string, unknown>;
}

/** ① plan_review：计划确认（内置）。 */
const PlanReviewCard: CardComponent = ({ ctx }) => {
  const plan = (ctx.inner.plan ?? []) as PlanItem[];
  return (
    <Shell severity="warn" title="Agent 生成了执行计划，请确认">
      {plan.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          {plan.map((step, i) => (
            <div key={step.seq ?? i} style={{ fontSize: 13, marginBottom: 2 }}>
              {step.seq ?? i + 1}. {step.text}
            </div>
          ))}
        </div>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <Button type="primary" icon={<CheckOutlined />} onClick={() => ctx.onConfirm("approved")}>
          批准计划
        </Button>
        <Button icon={<CloseOutlined />} onClick={ctx.onReject}>
          拒绝
        </Button>
      </div>
    </Shell>
  );
};

/** ② high_risk_tool：高危工具确认（内置，dangerous 红色强调）。
 * 面向普通用户的人话表述：说清「要做什么、为什么危险」；原始参数折叠为
 * 「查看将提交的数据」技术细节（默认收起）——把 JSON 平铺给用户没有意义。
 */
const RISK_HUMAN: Record<string, { label: string; desc: string }> = {
  write: {
    label: "写入操作",
    desc: "该操作会向外部系统提交或修改数据，提交后即实际生效，通常无法撤销。",
  },
  dangerous: {
    label: "高危操作",
    desc: "该操作风险较高（可能修改、删除重要数据或执行敏感动作），请仔细确认后再执行。",
  },
};

// 常见业务工具的人话说明（未命中的走通用描述）
const TOOL_HUMAN: Record<string, string> = {
  procurement_trigger: "向外部采购系统提交报价单并触发解析",
  procurement_submit_decision: "向外部采购系统写入你的决策结果",
};

const HighRiskToolCard: CardComponent = ({ ctx }) => {
  const [showArgs, setShowArgs] = useState(false);
  const calls = (ctx.inner.calls ?? []) as RiskyCall[];
  const riskLevels = (ctx.inner.risk_levels ?? {}) as Record<string, string>;
  const hasDangerous = Object.values(riskLevels).includes("dangerous");
  return (
    <Shell
      severity="danger"
      title="Agent 请求执行敏感操作，需要你确认"
      icon={<ExclamationCircleOutlined style={{ color: "var(--ant-color-error)" }} />}
    >
      {calls.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          {calls.map((c, i) => {
            const level = riskLevels[c.name] ?? "write";
            const risk = RISK_HUMAN[level] ?? RISK_HUMAN.write;
            const what = TOOL_HUMAN[c.name] ?? `调用「${c.name}」工具并提交数据`;
            const argsJson = JSON.stringify(c.args, null, 2);
            return (
              <div key={i} style={{ marginBottom: 10 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                  <Typography.Text strong style={{ fontSize: 13 }}>
                    {what}
                  </Typography.Text>
                  <Tag color={level === "dangerous" ? "error" : "warning"} style={{ marginInlineEnd: 0 }}>
                    {risk.label}
                  </Tag>
                </div>
                <div
                  style={{
                    fontSize: 13,
                    color: "var(--ant-color-text-secondary)",
                    margin: "4px 0 6px",
                    lineHeight: 1.6,
                  }}
                >
                  {risk.desc}
                </div>
                {/* 数据细节：默认收起，点开才看原始参数（用户不是程序员） */}
                <Button
                  type="link"
                  size="small"
                  style={{ padding: 0, fontSize: 12, height: "auto" }}
                  onClick={() => setShowArgs((v) => !v)}
                >
                  {showArgs ? "收起数据详情" : `查看将提交的数据（约 ${argsJson.length} 字符）`}
                </Button>
                {showArgs && (
                  <pre
                    className="font-mono-tight"
                    style={{
                      margin: "4px 0 0",
                      padding: "6px 8px",
                      background: "var(--ant-color-fill-quaternary)",
                      borderRadius: 4,
                      fontSize: 12,
                      whiteSpace: "pre-wrap",
                      maxHeight: 240,
                      overflow: "auto",
                    }}
                  >
                    {argsJson.slice(0, 3000)}
                    {argsJson.length > 3000 ? "\n…（已截断）" : ""}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <Button
          type="primary"
          danger
          icon={<CheckOutlined />}
          onClick={() => ctx.onConfirm("approved")}
        >
          确认执行
        </Button>
        <Button icon={<CloseOutlined />} danger={hasDangerous} onClick={ctx.onReject}>
          拒绝
        </Button>
      </div>
    </Shell>
  );
};

/** ③ subtask_clarification：支线澄清（内置，ask_user 文本答复回注，ADR-24）。 */
const SubtaskClarificationCard: CardComponent = ({ ctx }) => {
  const [reply, setReply] = useState("");
  const question = String(ctx.inner.question ?? "");
  const title = `支线子任务：${String(ctx.inner.title ?? "需要你确认")}`;
  return (
    <Shell
      severity="warn"
      title={title}
      icon={<QuestionCircleOutlined style={{ color: "var(--ant-color-warning)" }} />}
    >
      <div style={{ marginBottom: 8 }}>
        <div style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{question}</div>
        <Input.TextArea
          style={{ marginTop: 8 }}
          rows={2}
          autoFocus
          placeholder="输入你的答复（答复会写入子任务记录）"
          value={reply}
          onChange={(e) => setReply(e.target.value)}
          onPressEnter={(e) => {
            if (!e.shiftKey && reply.trim()) ctx.onConfirm(reply.trim());
          }}
        />
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <Button
          type="primary"
          icon={<CheckOutlined />}
          disabled={!reply.trim()}
          onClick={() => ctx.onConfirm(reply.trim())}
        >
          提交答复并继续
        </Button>
        <Button icon={<CloseOutlined />} onClick={ctx.onReject}>
          先不答复（放弃本次执行）
        </Button>
      </div>
    </Shell>
  );
};

/** 卡片主体通用渲染：表格 / 列表 / 字段 / 扁平对象 / JSON 兜底（声明式模板用）。 */
function BodyView({ body }: { body: Record<string, unknown> }) {
  if (!body || Object.keys(body).length === 0) return null;

  const table = body.table as
    | { columns?: { key: string; title: string }[]; rows?: Record<string, unknown>[] }
    | undefined;
  if (table?.rows && table.rows.length > 0) {
    const rows = table.rows.map((r, i) => ({ ...r, _k: i }));
    const columns = (table.columns ?? []).map((c) => ({
      title: c.title,
      dataIndex: c.key,
      key: c.key,
    }));
    return (
      <Table
        size="small"
        rowKey="_k"
        style={{ marginBottom: 8 }}
        dataSource={rows}
        columns={columns}
        pagination={false}
        scroll={{ y: 200 }}
      />
    );
  }

  const items = body.items as unknown[] | undefined;
  if (Array.isArray(items) && items.length > 0) {
    return (
      <div style={{ marginBottom: 8 }}>
        {items.map((it, i) => (
          <div key={i} style={{ fontSize: 12, marginBottom: 2 }}>
            • {typeof it === "string" ? it : JSON.stringify(it)}
          </div>
        ))}
      </div>
    );
  }

  const fields = body.fields as { label: string; value: unknown }[] | undefined;
  if (Array.isArray(fields) && fields.length > 0) {
    return (
      <div style={{ marginBottom: 8 }}>
        {fields.map((f, i) => (
          <div key={i} style={{ fontSize: 12, marginBottom: 2 }}>
            <Typography.Text type="secondary">{f.label}：</Typography.Text>
            {String(f.value ?? "")}
          </div>
        ))}
      </div>
    );
  }

  const entries = Object.entries(body);
  if (entries.length > 0 && entries.every(([, v]) => typeof v !== "object" || v === null)) {
    return (
      <div style={{ marginBottom: 8 }}>
        {entries.map(([k, v]) => (
          <div key={k} style={{ fontSize: 12, marginBottom: 2 }}>
            <Typography.Text type="secondary">{k}：</Typography.Text>
            {String(v)}
          </div>
        ))}
      </div>
    );
  }

  return (
    <pre
      className="font-mono-tight"
      style={{
        margin: "0 0 8px",
        padding: "6px 8px",
        background: "var(--ant-color-fill-quaternary)",
        borderRadius: 4,
        fontSize: 12,
        whiteSpace: "pre-wrap",
        maxHeight: 200,
        overflow: "auto",
      }}
    >
      {JSON.stringify(body, null, 2)}
    </pre>
  );
}

/** 按 kind 分派动作点击（interactive_decision / 自定义声明式卡共用）。 */
function dispatchAction(action: CardAction, ctx: CardContext) {
  switch (action.kind) {
    case "open_sidebar": {
      const sidebar = (ctx.inner.sidebar ?? {}) as SidebarDescriptor;
      ctx.onOpenSidebar({
        ...sidebar,
        title: sidebar.title ?? String(ctx.inner.title ?? "交互决策"),
        run_id: sidebar.run_id ?? ctx.runId,
        step_id: sidebar.step_id ?? (ctx.inner.step_id as string | undefined),
        idempotency_key:
          sidebar.idempotency_key ?? (ctx.inner.idempotency_key as string | undefined),
      });
      break;
    }
    case "reject":
      ctx.onReject();
      break;
    case "confirm":
      ctx.onConfirm(action.key === "confirm" ? "approved" : action.key);
      break;
    case "custom":
    default:
      ctx.onConfirm(action.key);
      break;
  }
}

function actionIcon(kind: CardAction["kind"]): ReactNode {
  if (kind === "open_sidebar") return <ExportOutlined />;
  if (kind === "reject") return <CloseOutlined />;
  return <CheckOutlined />;
}

/** 声明式卡面：title/summary/severity/body + actions 数组（interactive_decision 与自定义卡共用）。 */
function DeclarativeCard({ ctx, fallbackTitle }: { ctx: CardContext; fallbackTitle: string }) {
  const title = String(ctx.inner.title ?? fallbackTitle);
  const summary = String(ctx.inner.summary ?? "");
  const severity = normSeverity(ctx.inner.severity, "info");
  const body = (ctx.inner.body ?? {}) as Record<string, unknown>;
  const rawActions = ctx.inner.actions as CardAction[] | undefined;
  const actions: CardAction[] =
    rawActions && rawActions.length > 0
      ? rawActions
      : [
          { key: "confirm", label: "确认", kind: "confirm", style: "primary" },
          { key: "reject", label: "取消", kind: "reject" },
        ];
  return (
    <Shell
      severity={severity}
      title={title}
      icon={<InteractionOutlined style={{ color: SEVERITY_META[severity].color }} />}
    >
      {summary && (
        <div style={{ fontSize: 13, marginBottom: 8, whiteSpace: "pre-wrap" }}>{summary}</div>
      )}
      <BodyView body={body} />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {actions.map((a) => (
          <Button
            key={a.key}
            type={a.style === "primary" ? "primary" : "default"}
            danger={a.style === "danger"}
            icon={actionIcon(a.kind)}
            onClick={() => dispatchAction(a, ctx)}
          >
            {a.label}
          </Button>
        ))}
      </div>
    </Shell>
  );
}

/** ⑤ interactive_decision：交互决策卡（内置，带 sidebar，侧边栏人在环通用载体）。 */
const InteractiveDecisionCard: CardComponent = ({ ctx }) => (
  <DeclarativeCard ctx={ctx} fallbackTitle="需要你处理一项决策" />
);

/** 自定义卡兜底（声明式模板）：未命中注册表的 card_type 通用渲染。 */
const GenericCard: CardComponent = ({ ctx }) => (
  <DeclarativeCard ctx={ctx} fallbackTitle={`需要确认（${ctx.cardType}）`} />
);

/** 结果信封 outcome → 展示文案（done/partial/failed/blocked）。 */
const OUTCOME_LABEL: Record<string, string> = {
  done: "已完成",
  partial: "部分完成",
  failed: "失败",
  blocked: "受阻",
};

/** 非 done 的机器可读原因 → 人话（后端 reason 字段，值域见方案 §4 P0-5）。 */
const REASON_LABEL: Record<string, string> = {
  timeout: "执行超时（已保留部分结果）",
  verify_not_achieved: "验收未达成",
  budget_exhausted: "预算耗尽",
  cancelled: "已取消",
  aborted: "已中止",
};

function outcomeSeverity(outcome: string): Severity {
  if (outcome === "failed") return "danger";
  if (outcome === "partial" || outcome === "blocked") return "warn";
  return "info";
}

function fmtTokens(metrics: Record<string, unknown>): string {
  const tokens = Number(metrics.input_tokens ?? 0) + Number(metrics.output_tokens ?? 0);
  return tokens > 0 ? ` · token ${tokens.toLocaleString()}` : "";
}

/** 产物行：text/json 就地展开正文，file 下载落盘。 */
function ArtifactRow({ artifact }: { artifact: RunArtifactRef }) {
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isFile = artifact.kind === "file";

  async function open() {
    setBusy(true);
    try {
      if (isFile) {
        const blob = await artifactsApi.fetchContent(artifact.id);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = artifact.name || artifact.id;
        a.click();
        URL.revokeObjectURL(url);
        return;
      }
      const detail = await artifactsApi.get(artifact.id);
      const payload = detail.payload;
      setText(
        payload && "text" in payload
          ? String(payload.text)
          : JSON.stringify(payload ?? detail, null, 2),
      );
    } catch (e) {
      setText(`加载失败：${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ marginBottom: 4 }}>
      <Space size={6} wrap>
        <Typography.Text style={{ fontSize: 12 }}>{artifact.name || artifact.id}</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          {artifact.kind} · {fmtSize(artifact.size)}
        </Typography.Text>
        <Button size="small" type="link" loading={busy} onClick={open}>
          {isFile ? "下载" : text ? "收起" : "查看"}
        </Button>
      </Space>
      {text !== null && (
        <pre
          className="font-mono-tight"
          style={{
            fontSize: 12,
            margin: "4px 0 0",
            whiteSpace: "pre-wrap",
            maxHeight: 240,
            overflow: "auto",
          }}
        >
          {text.length > 4000 ? `${text.slice(0, 4000)}…` : text}
        </pre>
      )}
    </div>
  );
}

/**
 * ⑥ result：结果卡（内置，P0-5）。
 *
 * 只渲染**结果元信息**（outcome / reason / metrics / artifacts）：
 * 正文已作为 assistant 消息落库并渲染，卡面再渲染一次就是双渲染。
 * 卡本身进事件流（`card` 事件），因此历史重放不丢信息。
 */
const ResultCard: CardComponent = ({ ctx }) => {
  const inner = ctx.inner;
  const outcome = String(inner.outcome ?? "done");
  const severity = outcomeSeverity(outcome);
  const reason = String(inner.reason ?? "");
  const metrics = (inner.metrics ?? {}) as Record<string, unknown>;
  const artifactList = (inner.artifacts ?? []) as RunArtifactRef[];
  const iterations = Number(metrics.iterations ?? 0);
  const toolCalls = Number(metrics.tool_calls ?? 0);
  const activeMs = Number(metrics.active_ms ?? 0);
  return (
    <Shell
      severity={severity}
      title={`运行结果 · ${OUTCOME_LABEL[outcome] ?? outcome}`}
      icon={<InfoCircleOutlined style={{ color: SEVERITY_META[severity].color }} />}
    >
      {reason && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {REASON_LABEL[reason] ?? reason}
        </Typography.Text>
      )}
      <div style={{ fontSize: 11, color: "var(--ant-color-text-tertiary)", margin: "4px 0 8px" }}>
        {activeMs > 0 ? `活跃 ${(activeMs / 1000).toFixed(1)}s` : ""}
        {iterations > 0 ? ` · 迭代 ${iterations}` : ""}
        {toolCalls > 0 ? ` · 工具 ${toolCalls}` : ""}
        {fmtTokens(metrics)}
      </div>
      {artifactList.map((a) => (
        <ArtifactRow key={a.id} artifact={a} />
      ))}
    </Shell>
  );
};

/**
 * 卡片注册表：card_type → 渲染器。
 * 内置 5 类（plan_review/high_risk_tool/subtask_clarification/interactive_decision/result）；
 * task_switch_suggested 走独立 TaskSwitchCard（send_message 返回，非 SSE 确认事件）。
 * 业务自定义卡无需改此表：声明式模板自动落 GenericCard，plugin 卡面经 open_sidebar 走侧边栏。
 */
export const CARD_REGISTRY: Record<string, CardComponent> = {
  plan_review: PlanReviewCard,
  high_risk_tool: HighRiskToolCard,
  subtask_clarification: SubtaskClarificationCard,
  interactive_decision: InteractiveDecisionCard,
  result: ResultCard,
};

export default function CardRenderer({
  payload,
  runId,
  onConfirm,
  onReject,
  onOpenSidebar,
}: {
  payload: Record<string, unknown>;
  runId: string;
  /** 结果卡等只读卡片不传动作回调：缺省为空操作 */
  onConfirm?: (answer?: string) => void;
  onReject?: () => void;
  onOpenSidebar?: (sidebar: SidebarDescriptor) => void;
}) {
  const cardType = String(payload.reason ?? "generic");
  const inner = (payload.payload ?? {}) as Record<string, unknown>;
  const Renderer = CARD_REGISTRY[cardType] ?? GenericCard;
  const ctx: CardContext = {
    cardType,
    inner,
    runId,
    onConfirm: onConfirm ?? (() => {}),
    onReject: onReject ?? (() => {}),
    onOpenSidebar: onOpenSidebar ?? (() => {}),
  };
  return <Renderer ctx={ctx} />;
}
