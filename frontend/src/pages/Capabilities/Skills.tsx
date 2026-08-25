/**
 * 技能页（前端设计 §3.4）。
 * 「已注册技能」：type=skill 能力管理——直接添加（表单合成 SKILL.md）/ 编辑 / 删除。
 * 「沉淀提案」：运行中提炼的 SKILL.md 草稿 → 人工审核（批准/驳回 + 审核意见）。
 */
import { useState } from "react";
import {
  Tabs,
  List,
  Tag,
  Typography,
  Button,
  Modal,
  Input,
  Space,
  Empty,
  message,
  Table,
  Switch,
  Popconfirm,
} from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  FileTextOutlined,
  PlusOutlined,
  DeleteOutlined,
  EditOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { skillsApi } from "@/api/skills";
import { capabilitiesApi } from "@/api/capabilities";
import type { ProposalOut, CapabilityOut } from "@/api/types";
import MarkdownRenderer from "@/components/MarkdownRenderer";
import CapFormDrawer from "./CapFormDrawer";

const RISK_COLORS: Record<string, string> = {
  read: "green",
  write: "orange",
  dangerous: "red",
};

// ---------- 已注册技能 ----------

function RegisteredSkills() {
  const queryClient = useQueryClient();
  // Drawer 状态：open + 当前编辑对象（null = 添加模式）
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [formCap, setFormCap] = useState<CapabilityOut | null>(null);

  const { data: allCaps = [] } = useQuery({
    queryKey: ["capabilities", "all"],
    queryFn: () => capabilitiesApi.list(true),
  });
  const skillCaps = allCaps.filter((c) => c.type === "skill");

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
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setFormCap(null);
            setDrawerOpen(true);
          }}
        >
          添加技能
        </Button>
      </div>

      <Table<CapabilityOut>
        rowKey="id"
        dataSource={skillCaps}
        size="small"
        pagination={false}
        locale={{
          emptyText: (
            <Empty
              description="暂无已注册技能：可直接添加，或批准沉淀提案后自动转入"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ),
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
                <Popconfirm title="删除此技能？" onConfirm={() => deleteMutation.mutate(r.id)}>
                  <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <CapFormDrawer
        type="skill"
        cap={formCap}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      />
    </div>
  );
}

// ---------- 沉淀提案审核 ----------

const STATUS_META: Record<string, { color: string; label: string }> = {
  pending: { color: "warning", label: "待审核" },
  approved: { color: "success", label: "已批准" },
  rejected: { color: "error", label: "已驳回" },
};

function ProposalItem({
  proposal,
  onApprove,
  onReject,
  reviewing,
}: {
  proposal: ProposalOut;
  onApprove: (id: string, note: string) => void;
  onReject: (id: string, note: string) => void;
  reviewing: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [showNote, setShowNote] = useState<"approve" | "reject" | null>(null);
  const [note, setNote] = useState("");
  const st = STATUS_META[proposal.status] || { color: "default", label: proposal.status };

  return (
    <List.Item
      actions={[
        proposal.status === "pending" && (
          <Space key="actions">
            <Button
              size="small"
              type="primary"
              icon={<CheckOutlined />}
              loading={reviewing}
              onClick={() => { setNote(""); setShowNote("approve"); }}
            >
              批准
            </Button>
            <Button
              size="small"
              danger
              icon={<CloseOutlined />}
              onClick={() => { setNote(""); setShowNote("reject"); }}
            >
              驳回
            </Button>
          </Space>
        ),
        proposal.review_note && (
          <Typography.Text key="note" type="secondary" style={{ fontSize: 11 }}>
            审核意见: {proposal.review_note}
          </Typography.Text>
        ),
      ].filter(Boolean)}
    >
      <div style={{ width: "100%" }}>
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
          <Space>
            <Typography.Text strong>
              <FileTextOutlined /> 提案 {proposal.id.slice(0, 8)}
            </Typography.Text>
            <Tag color={st.color}>{st.label}</Tag>
            <Tag>{proposal.trigger === "success" ? "成功触发" : "失败触发"}</Tag>
          </Space>
          <Button type="link" size="small" onClick={() => setExpanded(!expanded)}>
            {expanded ? "收起草稿" : "查看草稿"}
          </Button>
        </div>
        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          Run: {proposal.run_id.slice(0, 8)} · {new Date(proposal.created_at).toLocaleString("zh-CN")}
          {proposal.reviewed_at && ` · 审核于 ${new Date(proposal.reviewed_at).toLocaleString("zh-CN")}`}
        </Typography.Text>

        {expanded && (
          <div
            style={{
              marginTop: 8,
              padding: 12,
              border: "1px solid rgba(128,128,128,0.25)",
              borderRadius: 6,
              maxHeight: 400,
              overflow: "auto",
              background: "var(--ant-color-bg-layout, transparent)",
            }}
          >
            <MarkdownRenderer content={proposal.draft_md} />
          </div>
        )}

        {/* 审核意见：轻量一次性确认（§4 规范豁免项） */}
        <Modal
          title={showNote === "approve" ? "批准提案" : "驳回提案"}
          open={showNote !== null}
          onCancel={() => setShowNote(null)}
          onOk={() => {
            if (showNote === "approve") onApprove(proposal.id, note);
            else onReject(proposal.id, note);
            setShowNote(null);
          }}
          okText="确认"
          okButtonProps={{ danger: showNote === "reject" }}
        >
          <Input.TextArea
            rows={3}
            placeholder={showNote === "approve" ? "审核意见（可选）" : "驳回原因（建议填写）"}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </Modal>
      </div>
    </List.Item>
  );
}

function ProposalsPanel() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string>("pending");

  const { data: proposals = [], isLoading } = useQuery({
    queryKey: ["skills-proposals", statusFilter],
    queryFn: () => skillsApi.proposals(statusFilter || undefined),
  });

  const approveMutation = useMutation({
    mutationFn: ({ id, note }: { id: string; note: string }) => skillsApi.approve(id, note || undefined),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills-proposals"] });
      message.success("已批准（技能已转入注册列表）");
    },
    onError: () => message.error("批准失败"),
  });

  const rejectMutation = useMutation({
    mutationFn: ({ id, note }: { id: string; note: string }) => skillsApi.reject(id, note || undefined),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills-proposals"] });
      message.success("已驳回");
    },
    onError: () => message.error("驳回失败"),
  });

  const reviewing = approveMutation.isPending || rejectMutation.isPending;

  return (
    <div>
      <Tabs
        activeKey={statusFilter}
        onChange={setStatusFilter}
        items={[
          { key: "pending", label: "待审核" },
          { key: "approved", label: "已批准" },
          { key: "rejected", label: "已驳回" },
          { key: "", label: "全部" },
        ]}
      />

      <List
        loading={isLoading}
        dataSource={proposals}
        locale={{ emptyText: <Empty description="暂无提案" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        renderItem={(p) => (
          <ProposalItem
            proposal={p}
            reviewing={reviewing}
            onApprove={(id, note) => approveMutation.mutate({ id, note })}
            onReject={(id, note) => rejectMutation.mutate({ id, note })}
          />
        )}
      />
    </div>
  );
}

// ---------- 页面 ----------

export default function SkillsPage() {
  const [tab, setTab] = useState("registered");

  return (
    <div>
      <Typography.Title level={5} style={{ marginBottom: 12 }}>
        <FileTextOutlined /> 技能
      </Typography.Title>

      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: "registered", label: "已注册技能", children: <RegisteredSkills /> },
          { key: "proposals", label: "沉淀提案", children: <ProposalsPanel /> },
        ]}
      />
    </div>
  );
}
