/**
 * Worker 列表页（/capabilities/workers）：Worker 文件包管理入口。
 * 每个 Worker = data/workers/<名>/ 文件包（WORKER.md 为唯一权威）；
 * 版本手动构建（编辑文件不产生新版本），启停即时生效。
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message as antdMessage,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnsType } from "antd/es/table";
import { workersApi } from "@/api/workers";
import type { WorkerOut } from "@/api/types";

export default function WorkersPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", description: "" });

  const { data: workers = [], isLoading } = useQuery({
    queryKey: ["workers"],
    queryFn: () => workersApi.list(),
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["workers"] });

  const createMutation = useMutation({
    mutationFn: () =>
      workersApi.create({ name: form.name.trim(), description: form.description.trim() }),
    onSuccess: (w) => {
      invalidate();
      setCreating(false);
      setForm({ name: "", description: "" });
      antdMessage.success(`Worker「${w.name}」已创建（v1 脚手架）`);
      navigate(`/capabilities/workers/${encodeURIComponent(w.name)}`);
    },
    onError: (e: Error) => antdMessage.error(e.message || "创建失败"),
  });

  const toggleMutation = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      workersApi.patch(name, { enabled }),
    onSuccess: invalidate,
    onError: (e: Error) => antdMessage.error(e.message || "操作失败"),
  });

  const buildMutation = useMutation({
    mutationFn: (name: string) => workersApi.buildVersion(name),
    onSuccess: (r) => {
      invalidate();
      antdMessage.success(`已构建新版本 ${r.version}（复制自 ${r.copied_from}）`);
    },
    onError: (e: Error) => antdMessage.error(e.message || "构建失败"),
  });

  const delMutation = useMutation({
    mutationFn: (name: string) => workersApi.del(name),
    onSuccess: () => {
      invalidate();
      antdMessage.success("Worker 已删除（任务实例保留，降级显示名称）");
    },
    onError: (e: Error) => antdMessage.error(e.message || "删除失败"),
  });

  const columns: ColumnsType<WorkerOut> = [
    {
      title: "名称",
      dataIndex: "name",
      render: (_, w) => (
        <Space size={8}>
          <span style={{ fontSize: 18 }}>{w.icon || "🧑‍🔧"}</span>
          <div>
            <Typography.Text
              strong
              style={{ cursor: "pointer" }}
              onClick={() => navigate(`/capabilities/workers/${encodeURIComponent(w.name)}`)}
            >
              {w.name}
            </Typography.Text>
            <div style={{ fontSize: 12, color: "var(--ant-color-text-tertiary)" }}>
              {w.description || "（未填写描述）"}
            </div>
          </div>
        </Space>
      ),
    },
    {
      title: "最新版本",
      dataIndex: "latest_version",
      width: 100,
      render: (v: string | null, w) =>
        v ? (
          <Space size={4}>
            <Tag style={{ fontSize: 11 }}>{v}</Tag>
            {w.pinned_version && w.pinned_version !== w.active_version && (
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                (active {w.active_version})
              </Typography.Text>
            )}
          </Space>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    {
      title: "版本数",
      dataIndex: "versions",
      width: 80,
      render: (v: WorkerOut["versions"]) => v.length,
    },
    {
      title: "状态",
      dataIndex: "enabled",
      width: 80,
      render: (enabled: boolean, w) => (
        <Switch
          size="small"
          checked={enabled}
          onChange={(checked) => toggleMutation.mutate({ name: w.name, enabled: checked })}
        />
      ),
    },
    {
      title: "操作",
      width: 220,
      render: (_, w) => (
        <Space size={4}>
          <Button
            size="small"
            onClick={() => navigate(`/capabilities/workers/${encodeURIComponent(w.name)}`)}
          >
            管理
          </Button>
          <Popconfirm
            title="构建新版本？"
            description={`复制当前生效版本 ${w.active_version ?? ""} 为下一版本，并把 active 指向新版本。`}
            okText="构建"
            onConfirm={() => buildMutation.mutate(w.name)}
          >
            <Button size="small" loading={buildMutation.isPending && buildMutation.variables === w.name}>
              构建新版本
            </Button>
          </Popconfirm>
          <Popconfirm
            title={`删除 Worker「${w.name}」？`}
            description="文件包整体删除；历史任务实例保留（降级显示名称）。"
            okText="删除"
            okButtonProps={{ danger: true }}
            onConfirm={() => delMutation.mutate(w.name)}
          >
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div className="page-action-bar">
        <div>
          <Typography.Text strong>Worker</Typography.Text>
          <div className="page-action-sub">
            每个 Worker 是一个文件包（WORKER.md 为唯一权威）；版本手动构建，编辑文件不产生新版本
          </div>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
          新建 Worker
        </Button>
      </div>

      <Table<WorkerOut>
        rowKey="name"
        size="middle"
        loading={isLoading}
        dataSource={workers}
        columns={columns}
        pagination={false}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="暂无 Worker：新建后生成 v1 文件包脚手架"
            />
          ),
        }}
      />

      <Modal
        title="新建 Worker"
        open={creating}
        onCancel={() => setCreating(false)}
        onOk={() => form.name.trim() && createMutation.mutate()}
        confirmLoading={createMutation.isPending}
        okText="创建"
        cancelText="取消"
        destroyOnClose
      >
        <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 8 }}>
          <Input
            placeholder="Worker 名称（= 文件包目录名，允许中文）"
            maxLength={128}
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
          />
          <Input.TextArea
            placeholder="一句话描述（写入 WORKER.md 头 description，意图识别用）"
            rows={2}
            maxLength={4000}
            value={form.description}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            创建即生成 v1 脚手架（WORKER.md + sub_workers/ + references/），创建后进入文件管理器编辑。
          </Typography.Text>
        </Space>
      </Modal>
    </div>
  );
}
