/**
 * 工具页（前端设计 §3.4）：type=tool 的能力管理。
 * 内置工具只读；外部工具可注册/编辑（右侧 Drawer 表单，§4 规范）。
 */
import { useState } from "react";
import { Table, Button, Tag, Switch, Space, Typography, message, Popconfirm } from "antd";
import { PlusOutlined, ToolOutlined, DeleteOutlined, EditOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { capabilitiesApi } from "@/api/capabilities";
import type { CapabilityOut } from "@/api/types";
import CapFormDrawer from "./CapFormDrawer";

const RISK_COLORS: Record<string, string> = {
  read: "green",
  write: "orange",
  dangerous: "red",
};

export default function ToolsPage() {
  const queryClient = useQueryClient();
  // Drawer 状态：open + 当前编辑对象（null = 注册模式）
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [formCap, setFormCap] = useState<CapabilityOut | null>(null);

  const { data: allCaps = [] } = useQuery({
    queryKey: ["capabilities", "all"],
    queryFn: () => capabilitiesApi.list(true),
  });
  const toolCaps = allCaps.filter((c) => c.type === "tool");

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
          <ToolOutlined /> 工具
        </Typography.Title>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setFormCap(null);
            setDrawerOpen(true);
          }}
        >
          注册工具
        </Button>
      </div>

      <Table<CapabilityOut>
        rowKey="id"
        dataSource={toolCaps}
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
            title: "类别",
            dataIndex: "category",
            width: 80,
            render: (cat: string) => (
              <Tag>{cat === "builtin" ? "内置" : cat === "internal" ? "内部" : "外部"}</Tag>
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
            title: "启用",
            dataIndex: "enabled",
            width: 60,
            render: (enabled: boolean, r) => (
              <Switch
                size="small"
                checked={enabled}
                disabled={r.category === "builtin"}
                onChange={(checked) => toggleEnabled.mutate({ id: r.id, enabled: checked })}
              />
            ),
          },
          {
            title: "",
            width: 90,
            render: (_, r) =>
              r.category !== "builtin" ? (
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
                  <Popconfirm title="删除此工具？" onConfirm={() => deleteMutation.mutate(r.id)}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
              ) : null,
          },
        ]}
      />

      <CapFormDrawer
        type="tool"
        cap={formCap}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      />
    </div>
  );
}
