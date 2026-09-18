/**
 * 卡片外壳与严重度着色：`CardRenderer` 及其各内置卡面（含 `StepInputCard`）共用。
 *
 * 单独成模块是为了避开循环依赖：卡面模块既要 `CardContext` 类型，也需要同一套外壳样式，
 * 若直接从 `CardRenderer` 取外壳，就会形成「渲染器 → 卡面 → 渲染器」的运行期环。
 */
import type { ComponentType, ReactNode } from "react";
import { Card, Space, Typography } from "antd";
import type { CardContext } from "./CardRenderer";

/** 一张卡片渲染器的签名（`CardContext` 为纯类型依赖，不产生运行期环）。 */
export type CardComponent = ComponentType<{ ctx: CardContext }>;

export type Severity = "info" | "warn" | "danger";

export const SEVERITY_META: Record<Severity, { color: string; bg: string }> = {
  info: { color: "var(--ant-color-primary)", bg: "rgba(22,119,255,0.06)" },
  warn: { color: "var(--ant-color-warning)", bg: "rgba(250,173,20,0.06)" },
  danger: { color: "var(--ant-color-error)", bg: "rgba(255,77,79,0.06)" },
};

export function normSeverity(v: unknown, fallback: Severity = "info"): Severity {
  const s = String(v ?? "").toLowerCase();
  return s === "danger" || s === "warn" || s === "info" ? s : fallback;
}

/** 卡片外壳：统一非模态样式（出现在输入框上方），按 severity 着色。 */
export function Shell({
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
