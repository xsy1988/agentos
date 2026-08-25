/**
 * 插件页（前端设计 §3.4）：type=plugin 的能力管理。
 * 注册/编辑走右侧 Drawer 表单（与 MCP 同构，定位是扩展插件）。
 */
import { useState } from "react";
import { Table, Button, Tag, Switch, Space, Typography, message, Popconfirm } from "antd";
import { PlusOutlined, AppstoreAddOutlined, DeleteOutlined, EditOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { capabilitiesApi } from "@/api/capabilities";
import type { CapabilityOut } from "@/api/types";
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

export default function PluginsPage() {
  const queryClient = useQueryClient();
  // Drawer 状态：open + 当前编辑对象（null = 注册模式）
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [formCap, setFormCap] = useState<CapabilityOut | null>(null);

  const { data: allCaps = [] } = useQuery({
    queryKey: ["capabilities", "all"],
    queryFn: () => capabilitiesApi.list(true),
  });
  const pluginCaps = allCaps.filter((c) => c.type === "plugin");

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
          <AppstoreAddOutlined /> 插件
        </Typography.Title>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setFormCap(null);
            setDrawerOpen(true);
          }}
        >
          注册插件
        </Button>
      </div>

      <Table<CapabilityOut>
        rowKey="id"
        dataSource={pluginCaps}
        size="small"
        pagination={false}
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
            title: "版本",
            dataIndex: "version",
            width: 70,
            render: (v: string) => <Typography.Text style={{ fontSize: 12 }}>{v}</Typography.Text>,
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
                <Popconfirm title="删除此插件？" onConfirm={() => deleteMutation.mutate(r.id)}>
                  <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <CapFormDrawer
        type="plugin"
        cap={formCap}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      />
    </div>
  );
}
