/**
 * MCP 服务页（前端设计 §3.4）。
 * 注册/编辑 MCP Server（右侧 Drawer，含冒烟报告）→ 工具级开关 → 健康状态。
 */
import { useState } from "react";
import { Table, Button, Tag, Switch, Space, Typography, message, Popconfirm } from "antd";
import { PlusOutlined, ApiOutlined, DeleteOutlined, EditOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { capabilitiesApi } from "@/api/capabilities";
import type { CapabilityOut, CapabilityToolOut } from "@/api/types";
import CapFormDrawer from "./CapFormDrawer";

const RISK_COLORS: Record<string, string> = {
  read: "green",
  write: "orange",
  dangerous: "red",
};

const HEALTH_META: Record<string, { color: string; label: string }> = {
  healthy: { color: "success", label: "健康" },
  degraded: { color: "warning", label: "降级" },
  down: { color: "error", label: "离线" },
  unknown: { color: "default", label: "未知" },
};

function ToolsCell({ capId }: { capId: string }) {
  const { data: tools = [] } = useQuery({
    queryKey: ["cap-tools", capId],
    queryFn: () => capabilitiesApi.tools(capId),
    staleTime: 30_000,
  });
  if (tools.length === 0) return <Typography.Text type="secondary">—</Typography.Text>;
  return <Typography.Text>{tools.length} 个工具</Typography.Text>;
}

function ToolsToggle({ capId }: { capId: string }) {
  const queryClient = useQueryClient();
  const { data: tools = [] } = useQuery({
    queryKey: ["cap-tools", capId],
    queryFn: () => capabilitiesApi.tools(capId),
  });
  const toggleMutation = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      capabilitiesApi.setTool(capId, name, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["cap-tools", capId] }),
  });
  if (tools.length === 0) return <Typography.Text type="secondary">无工具</Typography.Text>;
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      {tools.map((t: CapabilityToolOut) => (
        <div key={t.tool_name} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <Space size={4}>
            <Typography.Text style={{ fontSize: 13 }}>{t.tool_name}</Typography.Text>
            {t.description && (
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                {t.description.slice(0, 60)}
              </Typography.Text>
            )}
          </Space>
          <Switch
            size="small"
            checked={t.enabled}
            loading={toggleMutation.isPending}
            onChange={(checked) => toggleMutation.mutate({ name: t.tool_name, enabled: checked })}
          />
        </div>
      ))}
    </Space>
  );
}

export default function McpServicesPage() {
  const queryClient = useQueryClient();
  // Drawer 状态：open + 当前编辑对象（null = 注册模式）
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [formCap, setFormCap] = useState<CapabilityOut | null>(null);

  const { data: allCaps = [] } = useQuery({
    queryKey: ["capabilities", "all"],
    queryFn: () => capabilitiesApi.list(true),
  });
  const mcpCaps = allCaps.filter((c) => c.type === "mcp");

  const deleteMutation = useMutation({
    mutationFn: (id: string) => capabilitiesApi.del(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["capabilities"] });
      message.success("已删除");
    },
  });

  const toggleEnabled = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      capabilitiesApi.update(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["capabilities"] }),
  });

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between" }}>
        <Typography.Title level={5} style={{ margin: 0 }}>
          <ApiOutlined /> MCP 服务
        </Typography.Title>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setFormCap(null);
            setDrawerOpen(true);
          }}
        >
          注册 MCP Server
        </Button>
      </div>

      <Table<CapabilityOut>
        rowKey="id"
        dataSource={mcpCaps}
        size="small"
        pagination={false}
        expandable={{
          expandedRowRender: (record) => <ToolsToggle capId={record.id} />,
        }}
        columns={[
          {
            title: "名称",
            dataIndex: "name",
            render: (name: string, r) => (
              <Space direction="vertical" size={0}>
                <Typography.Text strong>{name}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  {r.description}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: "传输",
            dataIndex: "payload",
            width: 80,
            render: (payload: Record<string, unknown>) => (
              <Tag>{(payload.transport as string) || "—"}</Tag>
            ),
          },
          {
            title: "工具",
            width: 80,
            render: (_, r) => <ToolsCell capId={r.id} />,
          },
          {
            title: "风险",
            dataIndex: "risk_level",
            width: 70,
            render: (risk: string) => <Tag color={RISK_COLORS[risk]}>{risk}</Tag>,
          },
          {
            title: "健康",
            dataIndex: "health_status",
            width: 80,
            render: (status: string) => {
              const meta = HEALTH_META[status] || HEALTH_META.unknown;
              return <Tag color={meta.color}>{meta.label}</Tag>;
            },
          },
          {
            title: "启用",
            dataIndex: "enabled",
            width: 60,
            render: (enabled: boolean, r) => (
              <Switch
                size="small"
                checked={enabled}
                onChange={(checked) => toggleEnabled.mutate({ id: r.id, enabled: checked })}
              />
            ),
          },
          {
            title: "",
            width: 90,
            render: (_, r) => (
              <Space size={0}>
                <Button
                  size="small"
                  type="text"
                  icon={<EditOutlined />}
                  onClick={() => {
                    setFormCap(r);
                    setDrawerOpen(true);
                  }}
                />
                <Popconfirm title="删除此能力？" onConfirm={() => deleteMutation.mutate(r.id)}>
                  <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <CapFormDrawer
        type="mcp"
        cap={formCap}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      />
    </div>
  );
}
