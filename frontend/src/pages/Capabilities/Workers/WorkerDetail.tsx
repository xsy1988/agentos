/**
 * Worker 文件管理器（/capabilities/workers/:name）。
 * 左：版本内文件树（WORKER.md / sub_workers/* / references/* / tests/*）
 * 右：编辑器（active 版本可编辑，历史版本只读展示；保存不产生新版本）
 * 底：工具引用清单校验（WORKER.md capabilities ↔ 平台已注册能力）。
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Button,
  Dropdown,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  Tree,
  Typography,
  message as antdMessage,
} from "antd";
import type { DataNode } from "antd/es/tree";
import {
  ArrowLeftOutlined,
  CopyOutlined,
  DeleteOutlined,
  FileAddOutlined,
  FolderAddOutlined,
  ReloadOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { workersApi } from "@/api/workers";
import type { FileNodeOut } from "@/api/types";
import FormDrawer from "@/components/FormDrawer";

/** FileNodeOut → antd Tree DataNode（文件路径作 key；目录 default 展开一层） */
function toTreeNodes(node: FileNodeOut, prefix: string): DataNode[] {
  return node.children
    .slice()
    .sort((a, b) => (a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1))
    .map((child) => {
      const path = prefix ? `${prefix}/${child.name}` : child.name;
      return child.type === "dir"
        ? {
            key: path,
            title: <span style={{ fontSize: 12 }}>{child.name}</span>,
            children: toTreeNodes(child, path),
          }
        : {
            key: path,
            title: (
              <span
                style={{ fontSize: 12 }}
                className={child.name === "WORKER.md" ? "font-mono-tight" : undefined}
              >
                {child.name}
              </span>
            ),
            isLeaf: true,
          };
    });
}

export default function WorkerDetailPage() {
  const { name } = useParams<{ name: string }>();
  const workerName = decodeURIComponent(name ?? "");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [version, setVersion] = useState<string | undefined>(undefined); // undefined = 生效版本
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [buildOpen, setBuildOpen] = useState(false);
  const [newSubOpen, setNewSubOpen] = useState(false);
  const [newSub, setNewSub] = useState({ name: "", kind: "main" as "main" | "branch" });
  const [newFileOpen, setNewFileOpen] = useState(false);
  const [newFile, setNewFile] = useState("");

  const { data: worker } = useQuery({
    queryKey: ["worker", workerName],
    queryFn: () => workersApi.get(workerName),
    enabled: !!workerName,
  });

  const activeVersion = worker?.active_version ?? undefined;
  const viewing = version ?? activeVersion; // 当前查看版本（undefined 未加载）
  const isHistory = !!worker && !!viewing && viewing !== activeVersion;

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["worker", workerName] });
    queryClient.invalidateQueries({ queryKey: ["worker-tree", workerName] });
    queryClient.invalidateQueries({ queryKey: ["workers"] });
  };

  const { data: tree, isLoading: treeLoading } = useQuery({
    queryKey: ["worker-tree", workerName, viewing],
    queryFn: () => workersApi.tree(workerName, viewing),
    enabled: !!workerName && !!viewing,
  });

  const { data: file } = useQuery({
    queryKey: ["worker-file", workerName, viewing, selectedFile],
    queryFn: () => workersApi.getFile(workerName, selectedFile!, viewing),
    enabled: !!selectedFile,
  });

  useEffect(() => {
    if (file && !dirty) setContent(file.content);
  }, [file, dirty]);

  const saveMutation = useMutation({
    mutationFn: () => workersApi.putFile(workerName, selectedFile!, content, viewing),
    onSuccess: () => {
      setDirty(false);
      queryClient.invalidateQueries({ queryKey: ["worker-file", workerName] });
      invalidate();
      antdMessage.success("已保存（编辑文件不产生新版本）");
    },
    onError: (e: Error) => antdMessage.error(e.message || "保存失败"),
  });

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) => workersApi.patch(workerName, { enabled }),
    onSuccess: invalidate,
    onError: (e: Error) => antdMessage.error(e.message || "操作失败"),
  });

  const setActiveMutation = useMutation({
    mutationFn: (v: string) => workersApi.patch(workerName, { active_version: v }),
    onSuccess: (_data, v) => {
      setVersion(undefined);
      invalidate();
      antdMessage.success(`active 版本已切换到 ${v}`);
    },
    onError: (e: Error) => antdMessage.error(e.message || "切换失败"),
  });

  const buildMutation = useMutation({
    mutationFn: () => workersApi.buildVersion(workerName),
    onSuccess: (r) => {
      setBuildOpen(false);
      setVersion(undefined);
      invalidate();
      antdMessage.success(`已构建新版本 ${r.version}（复制自 ${r.copied_from}）`);
    },
    onError: (e: Error) => antdMessage.error(e.message || "构建失败"),
  });

  const deleteVersionMutation = useMutation({
    mutationFn: (v: string) => workersApi.deleteVersion(workerName, v),
    onSuccess: () => {
      setVersion(undefined);
      invalidate();
      antdMessage.success("版本已删除");
    },
    onError: (e: Error) => antdMessage.error(e.message || "删除失败"),
  });

  const delFileMutation = useMutation({
    mutationFn: (path: string) => workersApi.deleteFile(workerName, path, viewing),
    onSuccess: (_, path) => {
      if (selectedFile === path) {
        setSelectedFile(null);
        setDirty(false);
      }
      invalidate();
      antdMessage.success("已删除");
    },
    onError: (e: Error) => antdMessage.error(e.message || "删除失败"),
  });

  const createSubMutation = useMutation({
    mutationFn: () =>
      workersApi.createSubWorker(workerName, { name: newSub.name.trim(), kind: newSub.kind }),
    onSuccess: (s) => {
      setNewSubOpen(false);
      setNewSub({ name: "", kind: "main" });
      invalidate();
      antdMessage.success(`子任务「${s.name}」脚手架已创建`);
    },
    onError: (e: Error) => antdMessage.error(e.message || "创建失败"),
  });

  const createFileMutation = useMutation({
    mutationFn: () => workersApi.createFile(workerName, { path: newFile.trim(), content: "" }, viewing),
    onSuccess: () => {
      setNewFileOpen(false);
      const p = newFile.trim();
      setNewFile("");
      invalidate();
      setSelectedFile(p);
      antdMessage.success("文件已创建");
    },
    onError: (e: Error) => antdMessage.error(e.message || "创建失败"),
  });

  // 工具引用清单校验（跟随查看版本）
  const { data: capRefs } = useQuery({
    queryKey: ["worker-caps", workerName, viewing],
    queryFn: () => workersApi.capabilityRefs(workerName, viewing),
    enabled: !!workerName && !!viewing,
  });

  const treeData = useMemo(() => (tree ? toTreeNodes(tree, "") : []), [tree]);

  if (!worker) {
    return <Spin style={{ display: "block", margin: "80px auto" }} />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      {/* 顶部：Worker 名 + 启停 + 版本切换 + 构建新版本 */}
      <div className="page-action-bar">
        <div>
          <Space size={8}>
            <Button
              type="text"
              icon={<ArrowLeftOutlined />}
              onClick={() => navigate("/capabilities/workers")}
            />
            <span style={{ fontSize: 20 }}>{worker.icon || "🧑‍🔧"}</span>
            <Typography.Title level={4} style={{ margin: 0 }}>
              {worker.name}
            </Typography.Title>
            <Space size={4}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                启用
              </Typography.Text>
              <Switch
                size="small"
                checked={worker.enabled}
                onChange={(v) => toggleMutation.mutate(v)}
              />
            </Space>
          </Space>
          <div className="page-action-sub">
            {worker.description || "（未填写描述）"} · 子任务 {worker.sub_workers.length} · 引用能力{" "}
            {worker.capabilities.length}
          </div>
        </div>
        <Space>
          <Select
            size="small"
            style={{ width: 190 }}
            value={viewing ?? undefined}
            onChange={(v) => {
              setVersion(v);
              setSelectedFile(null);
              setDirty(false);
            }}
            options={worker.versions.map((v) => ({
              value: v.version,
              label: `${v.version}${v.active ? "（生效）" : v.latest ? "（最新）" : "（历史）"}`,
            }))}
          />
          {isHistory && (
            <Popconfirm
              title={`把 active 版本切回 ${viewing}？`}
              description="新任务将绑定该版本；已有任务不受影响。"
              okText="切换"
              onConfirm={() => setActiveMutation.mutate(viewing!)}
            >
              <Button size="small">设为生效版本</Button>
            </Popconfirm>
          )}
          {isHistory && worker.versions.length > 1 && (
            <Popconfirm
              title={`删除版本 ${viewing}？`}
              description="历史版本删除后不可恢复（生效版本不可删）。"
              okText="删除"
              okButtonProps={{ danger: true }}
              onConfirm={() => deleteVersionMutation.mutate(viewing!)}
            >
              <Button size="small" danger icon={<DeleteOutlined />}>
                删除此版本
              </Button>
            </Popconfirm>
          )}
          <Button
            size="small"
            type="primary"
            icon={<CopyOutlined />}
            onClick={() => setBuildOpen(true)}
          >
            构建新版本
          </Button>
        </Space>
      </div>

      {/* 主体：左文件树 + 右编辑器 */}
      <div style={{ flex: 1, display: "flex", minHeight: 0, borderTop: "1px solid var(--ant-color-border-secondary)" }}>
        <div
          style={{
            width: 250,
            flex: "none",
            borderRight: "1px solid var(--ant-color-border-secondary)",
            overflow: "auto",
            padding: "8px 4px",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "0 8px 6px" }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              文件包 {viewing}
            </Typography.Text>
            <Dropdown
              menu={{
                items: [
                  { key: "sub", icon: <FolderAddOutlined />, label: "新建子任务文件夹" },
                  { key: "file", icon: <FileAddOutlined />, label: "新建文件" },
                ],
                onClick: ({ key }) => (key === "sub" ? setNewSubOpen(true) : setNewFileOpen(true)),
              }}
              disabled={isHistory}
            >
              <Button type="text" size="small" icon={<FolderAddOutlined />} disabled={isHistory} />
            </Dropdown>
          </div>
          {treeLoading ? (
            <Spin size="small" style={{ display: "block", margin: "16px auto" }} />
          ) : treeData.length ? (
            <Tree
              blockNode
              showIcon={false}
              defaultExpandedKeys={["sub_workers", "references"]}
              selectedKeys={selectedFile ? [selectedFile] : []}
              treeData={treeData}
              onSelect={(keys) => {
                const k = String(keys[0] ?? "");
                setSelectedFile(k || null);
                setDirty(false);
              }}
            />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="空文件包" />
          )}
        </div>

        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, padding: "8px 12px" }}>
          {selectedFile ? (
            <>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 6,
                }}
              >
                <Space size={8}>
                  <Typography.Text className="font-mono-tight" style={{ fontSize: 12 }}>
                    {selectedFile}
                  </Typography.Text>
                  {isHistory && <Tag style={{ fontSize: 11 }}>历史版本只读</Tag>}
                  {dirty && <Tag color="warning" style={{ fontSize: 11 }}>未保存</Tag>}
                </Space>
                <Space size={4}>
                  <Popconfirm
                    title={`删除文件 ${selectedFile}？`}
                    description="WORKER.md 与顶层目录不可删除。"
                    okText="删除"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => delFileMutation.mutate(selectedFile)}
                  >
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                      disabled={isHistory || selectedFile === "WORKER.md"}
                    />
                  </Popconfirm>
                  <Button
                    type="text"
                    size="small"
                    icon={<ReloadOutlined />}
                    disabled={dirty}
                    onClick={() =>
                      queryClient.invalidateQueries({
                        queryKey: ["worker-file", workerName, viewing, selectedFile],
                      })
                    }
                  />
                  <Button
                    size="small"
                    type="primary"
                    icon={<SaveOutlined />}
                    disabled={!dirty || isHistory}
                    loading={saveMutation.isPending}
                    onClick={() => saveMutation.mutate()}
                  >
                    保存
                  </Button>
                </Space>
              </div>
              <Input.TextArea
                value={content}
                readOnly={isHistory}
                onChange={(e) => {
                  setContent(e.target.value);
                  setDirty(true);
                }}
                autoSize={false}
                style={{ flex: 1, resize: "none", fontSize: 12 }}
                className="font-mono-tight"
                placeholder="选择左侧文件查看/编辑"
              />
              <Typography.Text type="secondary" style={{ fontSize: 11, marginTop: 6 }}>
                {selectedFile.endsWith("WORKER.md")
                  ? "yaml 头（name/description/…）与正文 playbook；保存时校验 name 一致性与 kind 合法性。"
                  : "文本文件；保存不产生新版本（构建新版本请用顶部按钮）。"}
              </Typography.Text>
            </>
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="选择左侧文件查看 / 编辑"
              style={{ margin: "auto" }}
            />
          )}
        </div>
      </div>

      {/* 底部：工具引用清单校验 */}
      <div
        style={{
          flex: "none",
          borderTop: "1px solid var(--ant-color-border-secondary)",
          padding: "6px 12px",
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          工具引用清单（capabilities）：
        </Typography.Text>
        {capRefs ? (
          capRefs.references.length ? (
            capRefs.references.map((r) => (
              <Tooltip
                key={r.name}
                title={
                  r.found
                    ? `${r.type ?? ""} · 风险 ${r.risk_level ?? ""} · ${r.enabled ? "已启用" : "已停用"}`
                    : "平台未注册此能力（域提权不会命中）"
                }
              >
                <Tag
                  color={r.found ? (r.enabled ? "success" : "default") : "error"}
                  style={{ fontSize: 11, marginInlineEnd: 0 }}
                >
                  {r.name}
                  {r.found ? "" : " · 缺失"}
                </Tag>
              </Tooltip>
            ))
          ) : (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              未配置（该 Worker 只用通用能力）
            </Typography.Text>
          )
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            校验中…
          </Typography.Text>
        )}
      </div>

      {/* 构建新版本确认 */}
      <Modal
        title="构建新版本"
        open={buildOpen}
        onCancel={() => setBuildOpen(false)}
        onOk={() => buildMutation.mutate()}
        confirmLoading={buildMutation.isPending}
        okText="构建"
        cancelText="取消"
      >
        <Typography.Text style={{ fontSize: 13 }}>
          复制当前生效版本 <b>{activeVersion}</b> 的全部文件为下一版本，并把 active 指向新版本。
          编辑文件不产生新版本，演进请走此入口。
        </Typography.Text>
      </Modal>

      {/* 新建子任务文件夹 */}
      <FormDrawer
        title="新建子任务文件夹"
        open={newSubOpen}
        onClose={() => setNewSubOpen(false)}
        onOk={() => newSub.name.trim() && createSubMutation.mutate()}
        confirmLoading={createSubMutation.isPending}
        okText="创建"
        cancelText="取消"
        width={480}
        destroyOnClose
      >
        <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 8 }}>
          <Input
            placeholder="子任务名（= sub_workers 文件夹名）"
            maxLength={255}
            value={newSub.name}
            onChange={(e) => setNewSub((s) => ({ ...s, name: e.target.value }))}
          />
          <Select
            value={newSub.kind}
            style={{ width: 160 }}
            onChange={(v) => setNewSub((s) => ({ ...s, kind: v }))}
            options={[
              { value: "main", label: "主线（顺序推进）" },
              { value: "branch", label: "支线（按需触发）" },
            ]}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            生成 sub_workers/&lt;名&gt;/WORKER.md 脚手架（seq/kind/optional/capability_hint + playbook 正文）。
          </Typography.Text>
        </Space>
      </FormDrawer>

      {/* 新建文件 */}
      <FormDrawer
        title="新建文件"
        open={newFileOpen}
        onClose={() => setNewFileOpen(false)}
        onOk={() => newFile.trim() && createFileMutation.mutate()}
        confirmLoading={createFileMutation.isPending}
        okText="创建"
        cancelText="取消"
        width={480}
        destroyOnClose
      >
        <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 8 }}>
          <Input
            placeholder="相对路径，如 references/报价字段说明.md"
            maxLength={512}
            value={newFile}
            onChange={(e) => setNewFile(e.target.value)}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            仅文本文件（.md / .txt / .yaml / .json 等）；WORKER.md 已存在，请勿重复创建。
          </Typography.Text>
        </Space>
      </FormDrawer>
    </div>
  );
}
