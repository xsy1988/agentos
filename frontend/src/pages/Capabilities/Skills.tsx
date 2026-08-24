/**
 * 技能沉淀审核页（前端设计 §3.4）。
 * 查看运行中提炼的 SKILL.md 草稿 → 人工审核（批准/驳回 + 审核意见）。
 */
import { useState } from "react";
import { Tabs, List, Tag, Typography, Button, Modal, Input, Space, Empty, message } from "antd";
import { CheckOutlined, CloseOutlined, FileTextOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { skillsApi } from "@/api/skills";
import type { ProposalOut } from "@/api/types";
import MarkdownRenderer from "@/components/MarkdownRenderer";

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

export default function SkillsPage() {
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
      message.success("已批准");
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
      <Typography.Title level={5} style={{ marginBottom: 12 }}>
        <FileTextOutlined /> 技能沉淀
      </Typography.Title>

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
