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
  InteractionOutlined,
  QuestionCircleOutlined,
} from "@ant-design/icons";
import type { SidebarDescriptor } from "@/api/types";

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

/**
 * 卡片注册表：card_type → 渲染器。
 * 内置 4 类（plan_review/high_risk_tool/subtask_clarification/interactive_decision）；
 * task_switch_suggested 走独立 TaskSwitchCard（send_message 返回，非 SSE 确认事件）。
 * 业务自定义卡无需改此表：声明式模板自动落 GenericCard，plugin 卡面经 open_sidebar 走侧边栏。
 */
export const CARD_REGISTRY: Record<string, CardComponent> = {
  plan_review: PlanReviewCard,
  high_risk_tool: HighRiskToolCard,
  subtask_clarification: SubtaskClarificationCard,
  interactive_decision: InteractiveDecisionCard,
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
  onConfirm: (answer?: string) => void;
  onReject: () => void;
  onOpenSidebar: (sidebar: SidebarDescriptor) => void;
}) {
  const cardType = String(payload.reason ?? "generic");
  const inner = (payload.payload ?? {}) as Record<string, unknown>;
  const Renderer = CARD_REGISTRY[cardType] ?? GenericCard;
  const ctx: CardContext = { cardType, inner, runId, onConfirm, onReject, onOpenSidebar };
  return <Renderer ctx={ctx} />;
}
