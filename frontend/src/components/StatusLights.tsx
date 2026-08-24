/**
 * 三盏状态灯（前端设计 §4）：PG 连接 / 能力池健康 / 模型可用。
 * 异常变黄红，点开明细。
 */
import { Tooltip, Badge } from "antd";
import {
  DatabaseOutlined,
  ApiOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { CapabilityOut, ModelProviderOut } from "@/api/types";

function Light({
  icon,
  label,
  status,
  detail,
}: {
  icon: React.ReactNode;
  label: string;
  status: "ok" | "warn" | "error";
  detail: string;
}) {
  const color =
    status === "ok" ? "green" : status === "warn" ? "orange" : "red";
  return (
    <Tooltip title={`${label}：${detail}`}>
      <Badge dot color={color} offset={[-2, 2]}>
        <span style={{ opacity: status === "error" ? 0.5 : 1, fontSize: 16 }}>
          {icon}
        </span>
      </Badge>
    </Tooltip>
  );
}

export default function StatusLights() {
  // PG 连接：轮询 /health（不走 /api/v1 前缀，直接 fetch）
  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const res = await fetch("/health");
      if (!res.ok) return null;
      return res.json() as Promise<{ status: string }>;
    },
    refetchInterval: 30_000,
  });

  // 能力池健康：查询 capabilities
  const capsQuery = useQuery({
    queryKey: ["capabilities", "status"],
    queryFn: () =>
      api.get<CapabilityOut[]>("/capabilities").catch(() => []),
    refetchInterval: 30_000,
  });

  // 模型可用：查询 models
  const modelsQuery = useQuery({
    queryKey: ["models", "status"],
    queryFn: () =>
      api.get<ModelProviderOut[]>("/models", { kind: "llm" }).catch(() => []),
    refetchInterval: 30_000,
  });

  const pgStatus: "ok" | "warn" | "error" =
    healthQuery.data?.status === "ok" ? "ok" : "error";
  const pgDetail = pgStatus === "ok" ? "正常" : "连接失败";

  const caps = capsQuery.data ?? [];
  const enabledCaps = caps.filter((c) => c.enabled && c.type !== "skill");
  const unhealthy = enabledCaps.filter((c) => c.health_status === "unhealthy");
  const capStatus: "ok" | "warn" | "error" =
    unhealthy.length === 0 ? "ok" : unhealthy.length < enabledCaps.length ? "warn" : "error";
  const capDetail =
    enabledCaps.length === 0
      ? "无能力"
      : `${enabledCaps.length - unhealthy.length}/${enabledCaps.length} 在线`;

  const models = modelsQuery.data ?? [];
  const enabledModels = models.filter((m) => m.status === "enabled");
  const modelStatus: "ok" | "warn" | "error" =
    enabledModels.length > 0 ? "ok" : "error";
  const modelDetail =
    enabledModels.length > 0
      ? `${enabledModels.length} 个可用`
      : "无可用模型";

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <Light icon={<DatabaseOutlined />} label="PG 连接" status={pgStatus} detail={pgDetail} />
      <Light icon={<ApiOutlined />} label="能力池" status={capStatus} detail={capDetail} />
      <Light icon={<RobotOutlined />} label="模型" status={modelStatus} detail={modelDetail} />
    </div>
  );
}
