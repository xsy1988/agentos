/**
 * 知识库页（前端设计 §3.3）。
 * 左：目录树（CRUD + 拖拽文档换目录）
 * 右：文档列表 + 拖拽上传区（常驻）+ 管道进度 + 检索测试器
 */
import { useState } from "react";
import { Layout, Tree, Button, Upload, List, Tag, Typography, Empty, Input, message, Card } from "antd";
import {
  PlusOutlined,
  FolderOutlined,
  FileOutlined,
  ReloadOutlined,
  SearchOutlined,
  ExperimentOutlined,
} from "@ant-design/icons";
import type { UploadProps } from "antd";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { knowledgeApi } from "@/api/knowledge";
import { filesApi } from "@/api/files";
import type { FolderOut, DocOut, SearchHit } from "@/api/types";

const { Sider, Content } = Layout;

// 文档状态 → 颜色/标签
const DOC_STATUS: Record<string, { color: string; label: string }> = {
  uploaded: { color: "default", label: "已上传" },
  parsing: { color: "processing", label: "解析中" },
  chunking: { color: "processing", label: "切分中" },
  embedding: { color: "processing", label: "向量化中" },
  ready: { color: "success", label: "就绪" },
  failed: { color: "error", label: "失败" },
};

function buildTreeData(
  folders: FolderOut[],
  selectedId: string | null,
  onSelect: (id: string) => void,
): TreeDataNode[] {
  const map = new Map<string, TreeDataNode>();
  const roots: TreeDataNode[] = [];

  for (const f of folders) {
    map.set(f.id, {
      key: f.id,
      title: (
        <span
          style={{ cursor: "pointer", fontWeight: f.id === selectedId ? 600 : 400 }}
          onClick={() => onSelect(f.id)}
        >
          <FolderOutlined /> {f.name}
        </span>
      ),
      children: [],
    });
  }

  for (const f of folders) {
    const node = map.get(f.id)!;
    if (f.parent_id && map.has(f.parent_id)) {
      map.get(f.parent_id)!.children!.push(node);
    } else {
      roots.push(node);
    }
  }

  return roots;
}

interface TreeDataNode {
  key: string;
  title: React.ReactNode;
  children?: TreeDataNode[];
}

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
      style={{ marginTop: 12 }}
    >
      <Input.Search
        placeholder="输入检索查询…"
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

export default function KnowledgePage() {
  const queryClient = useQueryClient();
  const [selectedFolderId, setSelectedFolderId] = useState<string | null>(null);
  const [newFolderName, setNewFolderName] = useState("");
  const [showNewFolder, setShowNewFolder] = useState(false);

  // 目录树
  const { data: folders = [] } = useQuery({
    queryKey: ["kb-folders"],
    queryFn: knowledgeApi.folders,
  });

  // 文档列表
  const { data: docs = [], refetch: refetchDocs } = useQuery({
    queryKey: ["kb-docs", selectedFolderId],
    queryFn: () => knowledgeApi.docs(selectedFolderId ?? undefined),
    refetchInterval: (query) => {
      // 有 indexing 中文档时 5s 轮询
      const data = query.state.data as DocOut[] | undefined;
      if (data?.some((d) => ["parsing", "chunking", "embedding"].includes(d.status))) {
        return 5000;
      }
      return false;
    },
  });

  // 解析器健康
  const { data: parserHealth } = useQuery({
    queryKey: ["parser-health"],
    queryFn: () => knowledgeApi.parserHealth().catch(() => ({ healthy: false })),
    refetchInterval: 30_000,
  });

  // 创建文件夹
  const createFolderMutation = useMutation({
    mutationFn: (name: string) =>
      knowledgeApi.createFolder({
        name,
        parent_id: selectedFolderId ?? undefined,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["kb-folders"] });
      setShowNewFolder(false);
      setNewFolderName("");
      message.success("文件夹已创建");
    },
    onError: () => message.error("创建失败"),
  });

  // 重试文档
  const retryMutation = useMutation({
    mutationFn: (docId: string) => knowledgeApi.retryDoc(docId),
    onSuccess: () => {
      refetchDocs();
      message.success("已重试");
    },
  });

  // 删除文档
  const delDocMutation = useMutation({
    mutationFn: (docId: string) => knowledgeApi.delDoc(docId),
    onSuccess: () => {
      refetchDocs();
      message.success("已删除");
    },
  });

  // 上传 + 创建文档
  const uploadMutation = useMutation({
    mutationFn: async (file: File) => {
      if (!selectedFolderId) {
        message.warning("请先选择一个文件夹");
        return;
      }
      const fileOut = await filesApi.upload(file);
      await knowledgeApi.createDoc({
        file_id: fileOut.id,
        folder_id: selectedFolderId,
        title: fileOut.filename,
      });
    },
    onSuccess: () => {
      refetchDocs();
      message.success("文档已创建，管道处理中");
    },
    onError: () => message.error("上传失败"),
  });

  const uploadProps: UploadProps = {
    accept: ".pdf,.docx,.doc,.txt,.md,.pptx",
    showUploadList: false,
    customRequest: ({ file }) => {
      uploadMutation.mutate(file as File);
    },
    multiple: false,
  };

  const selectedFolder = folders.find((f) => f.id === selectedFolderId);

  return (
    <Layout style={{ height: "100%", background: "transparent" }}>
      {/* 左：目录树 */}
      <Sider
        width={240}
        style={{
          background: "var(--ant-color-bg-container)",
          borderRight: "1px solid rgba(128,128,128,0.2)",
          overflow: "auto",
        }}
      >
        <div style={{ padding: "8px" }}>
          <Button
            block
            icon={<PlusOutlined />}
            onClick={() => setShowNewFolder(true)}
          >
            新建文件夹
          </Button>
        </div>
        <Tree
          treeData={buildTreeData(folders, selectedFolderId, setSelectedFolderId)}
          defaultExpandAll
          selectedKeys={selectedFolderId ? [selectedFolderId] : []}
          style={{ padding: "0 4px" }}
        />
        {showNewFolder && (
          <div style={{ padding: "8px" }}>
            <Input
              size="small"
              placeholder="文件夹名称"
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.target.value)}
              onPressEnter={() => newFolderName && createFolderMutation.mutate(newFolderName)}
              addonAfter={
                <Button
                  type="link"
                  size="small"
                  onClick={() => newFolderName && createFolderMutation.mutate(newFolderName)}
                >
                  确定
                </Button>
              }
            />
          </div>
        )}
      </Sider>

      {/* 右：文档列表 + 上传区 + 检索测试器 */}
      <Content style={{ overflow: "auto", padding: "8px 16px" }}>
        {/* 解析器状态 */}
        <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 8 }}>
          <Tag color={parserHealth?.healthy ? "success" : "error"}>
            解析器 {parserHealth?.healthy ? "在线" : "离线"}
          </Tag>
          {selectedFolder && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              路径: {selectedFolder.path}
            </Typography.Text>
          )}
        </div>

        {/* 拖拽上传区 */}
        <Upload.Dragger
          {...uploadProps}
          style={{ marginBottom: 12, padding: 8 }}
        >
          <p style={{ margin: 0, fontSize: 13, color: "rgba(128,128,128,0.6)" }}>
            <FileOutlined /> 拖拽文件到此处上传（或点击选择）
          </p>
        </Upload.Dragger>

        {/* 文档列表 */}
        <List
          size="small"
          dataSource={docs}
          locale={{
            emptyText: <Empty description={selectedFolderId ? "暂无文档" : "请先选择文件夹"} image={Empty.PRESENTED_IMAGE_SIMPLE} />,
          }}
          renderItem={(doc) => {
            const st = DOC_STATUS[doc.status] ?? { color: "default", label: doc.status };
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
                      <Typography.Text type="danger" style={{ fontSize: 11 }}>
                        {doc.error}
                      </Typography.Text>
                    )}
                  </div>
                </div>
              </List.Item>
            );
          }}
        />

        {/* 检索测试器 */}
        <SearchTester />
      </Content>
    </Layout>
  );
}
