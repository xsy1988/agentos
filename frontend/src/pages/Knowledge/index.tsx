/**
 * 知识库页（两级结构，优化阶段重构）：
 * 第一级 /knowledge：文件夹卡片网格（每行 4 个）+ 新建文件夹 + 检索测试器
 * 第二级 /knowledge/:folderId：面包屑 + 子文件夹行 + 文档列表 + 上传 Drawer
 *   （「上传」按钮点击后右侧边栏展开，支持拖曳/点击多文件上传）
 */
import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Breadcrumb,
  Button,
  Card,
  Drawer,
  Empty,
  Input,
  List,
  message,
  Modal,
  Tag,
  Typography,
  Upload,
} from "antd";
import {
  ArrowLeftOutlined,
  ExperimentOutlined,
  FileOutlined,
  FolderOutlined,
  InboxOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import type { UploadFile, UploadProps } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { knowledgeApi } from "@/api/knowledge";
import { filesApi } from "@/api/files";
import type { DocOut, SearchHit } from "@/api/types";

// 文档状态 → 颜色/标签
const DOC_STATUS: Record<string, { color: string; label: string }> = {
  uploaded: { color: "default", label: "已上传" },
  parsing: { color: "processing", label: "解析中" },
  chunking: { color: "processing", label: "切分中" },
  embedding: { color: "processing", label: "向量化中" },
  ready: { color: "success", label: "就绪" },
  failed: { color: "error", label: "失败" },
};

const INDEXING = ["parsing", "chunking", "embedding"];

// ---------------------------------------------------------------- 检索测试器

function SearchTester() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);

  const handleSearch = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const hits = await knowledgeApi.search({ query: query.trim(), k: 5 });
      setResults(hits);
    } catch {
      message.error("检索失败");
    } finally {
      setSearching(false);
    }
  };

  return (
    <Card
      size="small"
      title={
        <span>
          <ExperimentOutlined /> 检索测试器
        </span>
      }
      style={{ marginTop: 16 }}
    >
      <Input.Search
        placeholder="输入检索查询，验证语义召回…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onSearch={handleSearch}
        loading={searching}
        enterButton={
          <span>
            <SearchOutlined /> 检索
          </span>
        }
      />
      {results.length > 0 && (
        <List
          size="small"
          style={{ marginTop: 8 }}
          dataSource={results}
          renderItem={(hit) => (
            <List.Item style={{ padding: "6px 0" }}>
              <div style={{ width: "100%" }}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <Typography.Text style={{ fontSize: 12 }} strong>
                    {hit.doc_title}
                  </Typography.Text>
                  <Tag style={{ fontSize: 10, margin: 0 }}>
                    {hit.score.toFixed(3)}
                  </Tag>
                </div>
                {hit.heading_path && (
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    {hit.heading_path}
                  </Typography.Text>
                )}
                <Typography.Paragraph
                  ellipsis={{ rows: 2 }}
                  style={{ margin: 0, fontSize: 12 }}
                >
                  {hit.content}
                </Typography.Paragraph>
              </div>
            </List.Item>
          )}
        />
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- 第一级：文件夹卡片网格

function FolderGrid() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [newName, setNewName] = useState("");

  const { data: folders = [] } = useQuery({
    queryKey: ["kb-folders"],
    queryFn: knowledgeApi.folders,
  });

  // 全量文档：按 folder_id 分组统计（卡片角标用）
  const { data: allDocs = [] } = useQuery({
    queryKey: ["kb-docs"],
    queryFn: () => knowledgeApi.docs(),
    refetchInterval: (query) => {
      const data = query.state.data as DocOut[] | undefined;
      return data?.some((d) => INDEXING.includes(d.status)) ? 5000 : false;
    },
  });

  const { data: parserHealth } = useQuery({
    queryKey: ["parser-health"],
    queryFn: () => knowledgeApi.parserHealth().catch(() => ({ healthy: false })),
    refetchInterval: 30_000,
  });

  const docCount = useMemo(() => {
    const m = new Map<string, number>();
    for (const d of allDocs) m.set(d.folder_id, (m.get(d.folder_id) ?? 0) + 1);
    return m;
  }, [allDocs]);

  const createFolderMutation = useMutation({
    mutationFn: (name: string) => knowledgeApi.createFolder({ name }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["kb-folders"] });
      setModalOpen(false);
      setNewName("");
      message.success("文件夹已创建");
    },
    onError: () => message.error("创建失败"),
  });

  // 第一级只展示顶层文件夹；子文件夹在第二级内导航
  const roots = folders.filter((f) => !f.parent_id);

  return (
    <div style={{ padding: "16px 20px", height: "100%", overflow: "auto" }}>
      {/* 顶栏：标题 + 解析器状态 + 新建 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 16,
        }}
      >
        <Typography.Title level={4} style={{ margin: 0 }}>
          知识库
        </Typography.Title>
        <Tag color={parserHealth?.healthy ? "success" : "error"}>
          解析器 {parserHealth?.healthy ? "在线" : "离线"}
        </Tag>
        <div style={{ flex: 1 }} />
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setModalOpen(true)}
        >
          新建文件夹
        </Button>
      </div>

      {/* 卡片网格：每行 4 个（窄屏自动降列） */}
      {roots.length === 0 ? (
        <Empty description="暂无文件夹，点击右上角新建" />
      ) : (
        <div className="kb-folder-grid">
          {roots.map((f) => {
            const n = docCount.get(f.id) ?? 0;
            const indexing = allDocs.some(
              (d) => d.folder_id === f.id && INDEXING.includes(d.status),
            );
            return (
              <Card
                key={f.id}
                hoverable
                className="kb-folder-card"
                onClick={() => navigate(`/knowledge/${f.id}`)}
              >
                <FolderOutlined className="kb-folder-icon" />
                <div className="kb-folder-name">{f.name}</div>
                <div className="kb-folder-meta">
                  {indexing && <Tag color="processing" style={{ fontSize: 10, margin: 0 }}>处理中</Tag>}
                  <span>
                    {n} 个文档
                  </span>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      <SearchTester />

      {/* 新建文件夹 */}
      <Modal
        title="新建文件夹"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => newName.trim() && createFolderMutation.mutate(newName.trim())}
        okText="创建"
        cancelText="取消"
        okButtonProps={{ disabled: !newName.trim() }}
        confirmLoading={createFolderMutation.isPending}
      >
        <Input
          placeholder="文件夹名称"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onPressEnter={() =>
            newName.trim() && createFolderMutation.mutate(newName.trim())
          }
          autoFocus
        />
      </Modal>
    </div>
  );
}

// ---------------------------------------------------------------- 第二级：文件夹详情

function FolderDetail({ folderId }: { folderId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [newName, setNewName] = useState("");

  const { data: folders = [] } = useQuery({
    queryKey: ["kb-folders"],
    queryFn: knowledgeApi.folders,
  });

  const { data: allDocs = [], refetch: refetchDocs } = useQuery({
    queryKey: ["kb-docs"],
    queryFn: () => knowledgeApi.docs(),
    refetchInterval: (query) => {
      const data = query.state.data as DocOut[] | undefined;
      return data?.some((d) => INDEXING.includes(d.status)) ? 5000 : false;
    },
  });

  const { data: parserHealth } = useQuery({
    queryKey: ["parser-health"],
    queryFn: () => knowledgeApi.parserHealth().catch(() => ({ healthy: false })),
    refetchInterval: 30_000,
  });

  const folder = folders.find((f) => f.id === folderId);
  // 子文件夹（嵌套结构在第二级内继续导航）
  const subFolders = folders.filter((f) => f.parent_id === folderId);
  const docs = useMemo(
    () => allDocs.filter((d) => d.folder_id === folderId),
    [allDocs, folderId],
  );

  const retryMutation = useMutation({
    mutationFn: (docId: string) => knowledgeApi.retryDoc(docId),
    onSuccess: () => {
      refetchDocs();
      message.success("已重试");
    },
  });

  const delDocMutation = useMutation({
    mutationFn: (docId: string) => knowledgeApi.delDoc(docId),
    onSuccess: () => {
      refetchDocs();
      message.success("已删除");
    },
  });

  const createFolderMutation = useMutation({
    mutationFn: (name: string) =>
      knowledgeApi.createFolder({ name, parent_id: folderId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["kb-folders"] });
      setModalOpen(false);
      setNewName("");
      message.success("子文件夹已创建");
    },
    onError: () => message.error("创建失败"),
  });

  // 上传：file → files.upload → kb/docs（管道后台处理）
  const uploadMutation = useMutation({
    mutationFn: async (file: File) => {
      const fileOut = await filesApi.upload(file);
      await knowledgeApi.createDoc({
        file_id: fileOut.id,
        folder_id: folderId,
        title: fileOut.filename,
      });
    },
    onSettled: () => refetchDocs(),
  });

  const uploadProps: UploadProps = {
    accept: ".pdf,.docx,.doc,.txt,.md,.pptx",
    multiple: true,
    fileList,
    customRequest: ({ file, onSuccess, onError }) => {
      uploadMutation.mutate(file as File, {
        onSuccess: () => onSuccess?.({}),
        onError: (e) => onError?.(e as Error),
      });
    },
    onChange: ({ fileList: fl }) => setFileList(fl),
  };

  return (
    <div style={{ padding: "16px 20px", height: "100%", overflow: "auto" }}>
      {/* 面包屑 + 上传按钮 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 16,
        }}
      >
        <Breadcrumb
          items={[
            {
              title: (
                <a onClick={() => navigate("/knowledge")}>
                  <ArrowLeftOutlined /> 知识库
                </a>
              ),
            },
            { title: folder?.name ?? "…" },
          ]}
        />
        <Tag
          color={parserHealth?.healthy ? "success" : "error"}
          style={{ margin: 0 }}
        >
          解析器 {parserHealth?.healthy ? "在线" : "离线"}
        </Tag>
        <div style={{ flex: 1 }} />
        <Button icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
          新建子文件夹
        </Button>
        <Button
          type="primary"
          icon={<UploadOutlined />}
          onClick={() => setUploadOpen(true)}
        >
          上传
        </Button>
      </div>

      {/* 子文件夹行（如有） */}
      {subFolders.length > 0 && (
        <div className="kb-subfolder-row">
          {subFolders.map((f) => (
            <Card
              key={f.id}
              size="small"
              hoverable
              className="kb-subfolder-card"
              onClick={() => navigate(`/knowledge/${f.id}`)}
            >
              <FolderOutlined /> {f.name}
              <span className="kb-subfolder-count">
                {allDocs.filter((d) => d.folder_id === f.id).length}
              </span>
            </Card>
          ))}
        </div>
      )}

      {/* 文档列表 */}
      <List
        size="small"
        dataSource={docs}
        locale={{
          emptyText: (
            <Empty
              description="暂无文档，点击右上角「上传」添加"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ),
        }}
        renderItem={(doc) => {
          const st = DOC_STATUS[doc.status] ?? {
            color: "default",
            label: doc.status,
          };
          return (
            <List.Item
              actions={[
                doc.status === "failed" && (
                  <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    onClick={() => retryMutation.mutate(doc.id)}
                  >
                    重试
                  </Button>
                ),
                <Button
                  size="small"
                  type="text"
                  danger
                  onClick={() => delDocMutation.mutate(doc.id)}
                >
                  删除
                </Button>,
              ].filter(Boolean)}
            >
              <div style={{ flex: 1, overflow: "hidden" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <FileOutlined style={{ color: "rgba(128,128,128,0.8)" }} />
                  <Typography.Text ellipsis style={{ fontSize: 13 }}>
                    {doc.title}
                  </Typography.Text>
                  <Tag color={st.color} style={{ margin: 0, fontSize: 10 }}>
                    {st.label}
                  </Tag>
                </div>
                <div style={{ display: "flex", gap: 12, marginTop: 2 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    {doc.chunk_count > 0 ? `${doc.chunk_count} 块` : "—"}
                  </Typography.Text>
                  {doc.error && (
                    <Typography.Text type="danger" style={{ fontSize: 11 }} ellipsis>
                      {doc.error}
                    </Typography.Text>
                  )}
                </div>
              </div>
            </List.Item>
          );
        }}
      />

      {/* 上传侧边栏 */}
      <Drawer
        title={
          <span>
            <UploadOutlined /> 上传到「{folder?.name ?? "…"}」
          </span>
        }
        placement="right"
        width={420}
        open={uploadOpen}
        onClose={() => {
          setUploadOpen(false);
          setFileList([]); // 关闭时清空队列，下次全新开始
        }}
      >
        <Upload.Dragger {...uploadProps}>
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽文件到此处上传</p>
          <p className="ant-upload-hint">
            支持 pdf / docx / doc / txt / md / pptx，单文件 ≤ 10MB，可多选
          </p>
        </Upload.Dragger>
        <Typography.Paragraph
          type="secondary"
          style={{ fontSize: 12, marginTop: 12 }}
        >
          上传后自动进入解析管道（解析 → 切分 → 向量化 → 就绪），失败可在列表中重试。
        </Typography.Paragraph>
      </Drawer>

      {/* 新建子文件夹 */}
      <Modal
        title={`在「${folder?.name ?? "…"}」下新建文件夹`}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => newName.trim() && createFolderMutation.mutate(newName.trim())}
        okText="创建"
        cancelText="取消"
        okButtonProps={{ disabled: !newName.trim() }}
        confirmLoading={createFolderMutation.isPending}
      >
        <Input
          placeholder="文件夹名称"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onPressEnter={() =>
            newName.trim() && createFolderMutation.mutate(newName.trim())
          }
          autoFocus
        />
      </Modal>
    </div>
  );
}

// ---------------------------------------------------------------- 入口

export default function KnowledgePage() {
  const { folderId } = useParams();
  return folderId ? <FolderDetail folderId={folderId} /> : <FolderGrid />;
}
