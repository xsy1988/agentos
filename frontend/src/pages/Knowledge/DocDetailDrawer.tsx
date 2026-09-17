/**
 * 文档详情 Drawer（管道状态可视化 + 切片管理）：
 * - Steps 步骤条：解析 → 切分 → 向量化 → 就绪（失败显示 error 全文）
 * - 进度行：已向量化 x/y 块 · embedding 模型；处理中 5s 轮询
 * - 工具栏：重新切分（自定义块大小/重叠）/ 重新向量化 / 失败断点重试
 * - Tabs：解析预览（docling 产物 markdown）+ 切片列表（分页/编辑/删除）
 */
import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Input,
  InputNumber,
  List,
  message,
  Popconfirm,
  Space,
  Steps,
  Tabs,
  Tag,
  Typography,
} from "antd";
import {
  DeleteOutlined,
  EditOutlined,
  ReloadOutlined,
  ScissorOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { knowledgeApi } from "@/api/knowledge";
import type { ChunkOut } from "@/api/types";
import FormDrawer from "@/components/FormDrawer";

const INDEXING = ["parsing", "chunking", "embedding"];
const PAGE = 50;

// status → Steps 当前步（0 解析 / 1 切分 / 2 向量化 / 3 就绪）
const STEP_INDEX: Record<string, number> = {
  uploaded: 0,
  parsing: 0,
  chunking: 1,
  embedding: 2,
  ready: 3,
  failed: 3,
};

export default function DocDetailDrawer({
  docId,
  onClose,
}: {
  docId: string | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  // 切片分页累积（重切/翻页都会追加；编辑/删除就地更新）
  const [chunks, setChunks] = useState<ChunkOut[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [editing, setEditing] = useState<ChunkOut | null>(null);
  const [editText, setEditText] = useState("");
  const [rechunkOpen, setRechunkOpen] = useState(false);
  const [targetTokens, setTargetTokens] = useState(512);
  const [overlapTokens, setOverlapTokens] = useState(64);

  const { data: doc } = useQuery({
    queryKey: ["kb-doc-detail", docId],
    queryFn: () => knowledgeApi.docDetail(docId as string),
    enabled: docId !== null,
    refetchInterval: (query) => {
      const d = query.state.data;
      return d && INDEXING.includes(d.status) ? 5000 : false;
    },
  });

  const { data: parse } = useQuery({
    queryKey: ["kb-parse", docId],
    queryFn: () => knowledgeApi.parsePreview(docId as string),
    enabled: docId !== null && (doc?.parse_exists ?? false),
  });

  const refreshAll = () => {
    queryClient.invalidateQueries({ queryKey: ["kb-docs"] });
    queryClient.invalidateQueries({ queryKey: ["kb-doc-detail", docId] });
  };

  // 换文档 / 重切后（chunk 数变化）重置分页
  useEffect(() => {
    setChunks([]);
    setHasMore(false);
  }, [docId, doc?.chunk_count]);

  const loadChunks = async (offset: number) => {
    if (!docId) return;
    const page = await knowledgeApi.chunks(docId, offset, PAGE);
    setHasMore(page.length >= PAGE);
    setChunks((cur) =>
      offset === 0
        ? page
        : [...cur, ...page.filter((p) => !cur.some((c) => c.id === p.id))],
    );
  };

  useEffect(() => {
    if (docId && doc && doc.chunk_count > 0) {
      loadChunks(0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId, doc?.status === "ready", doc?.chunk_count]);

  const rechunkMutation = useMutation({
    mutationFn: () =>
      knowledgeApi.rechunk(docId as string, {
        target_tokens: targetTokens,
        overlap_tokens: overlapTokens,
      }),
    onSuccess: () => {
      setRechunkOpen(false);
      message.success("已触发重新切分");
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const reembedMutation = useMutation({
    mutationFn: () => knowledgeApi.reembed(docId as string),
    onSuccess: () => {
      message.success("已触发重新向量化");
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const retryMutation = useMutation({
    mutationFn: () => knowledgeApi.retryDoc(docId as string),
    onSuccess: () => {
      message.success("已重试");
      refreshAll();
    },
  });

  const updateChunkMutation = useMutation({
    mutationFn: ({ id, content }: { id: string; content: string }) =>
      knowledgeApi.updateChunk(id, content),
    onSuccess: (updated) => {
      setChunks((cur) => cur.map((c) => (c.id === updated.id ? updated : c)));
      setEditing(null);
      message.success("切片已更新并重新向量化");
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const delChunkMutation = useMutation({
    mutationFn: (id: string) => knowledgeApi.delChunk(id),
    onSuccess: (_r, id) => {
      setChunks((cur) => cur.filter((c) => c.id !== id));
      message.success("切片已删除");
      refreshAll();
    },
  });

  if (!docId || !doc) {
    return <Drawer open={false} onClose={onClose} width={640} />;
  }

  const stepCurrent = STEP_INDEX[doc.status] ?? 0;
  const indexing = INDEXING.includes(doc.status);

  return (
    <Drawer
      open
      onClose={onClose}
      width={640}
      title={
        <Space>
          <Typography.Text strong ellipsis style={{ maxWidth: 360 }}>
            {doc.title}
          </Typography.Text>
          <Tag style={{ margin: 0 }}>{doc.source_type}</Tag>
        </Space>
      }
    >
      {/* 管道步骤条 */}
      <Steps
        size="small"
        current={stepCurrent}
        status={doc.status === "failed" ? "error" : undefined}
        items={[
          { title: "解析" },
          { title: "切分" },
          { title: "向量化" },
          { title: "就绪" },
        ]}
      />
      {doc.status === "failed" && doc.error && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 8 }}
          message="管道失败"
          description={doc.error}
        />
      )}

      {/* 进度行 */}
      <Typography.Text
        type="secondary"
        style={{ display: "block", marginTop: 8, fontSize: 12 }}
      >
        已向量化 {doc.embedded_count}/{doc.chunk_count} 块
        {doc.embedding_model ? ` · ${doc.embedding_model}` : ""}
        {indexing && " · 处理中，自动刷新…"}
      </Typography.Text>

      {/* 工具栏 */}
      <Space style={{ marginTop: 12, marginBottom: 8 }} wrap>
        <Button
          size="small"
          icon={<ScissorOutlined />}
          disabled={!doc.parse_exists || indexing || doc.status === "uploaded"}
          onClick={() => setRechunkOpen(true)}
        >
          重新切分
        </Button>
        <Button
          size="small"
          icon={<ThunderboltOutlined />}
          disabled={doc.status !== "ready"}
          loading={reembedMutation.isPending}
          onClick={() => reembedMutation.mutate()}
        >
          重新向量化
        </Button>
        {doc.status === "failed" && (
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={retryMutation.isPending}
            onClick={() => retryMutation.mutate()}
          >
            断点重试
          </Button>
        )}
      </Space>

      <Tabs
        items={[
          {
            key: "parse",
            label: "解析预览",
            children: doc.parse_exists ? (
              <pre
                style={{
                  fontSize: 12,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  maxHeight: "60vh",
                  overflow: "auto",
                  margin: 0,
                }}
              >
                {parse?.markdown ?? "加载中…"}
              </pre>
            ) : (
              <Typography.Text type="secondary">
                解析产物不存在（文档尚未解析完成或已被清理）
              </Typography.Text>
            ),
          },
          {
            key: "chunks",
            label: `切片 ${doc.chunk_count}`,
            children:
              doc.chunk_count === 0 ? (
                <Typography.Text type="secondary">
                  暂无切片（切分完成后展示）
                </Typography.Text>
              ) : (
                <>
                  <List
                    size="small"
                    dataSource={chunks}
                    renderItem={(c) => (
                      <List.Item
                        actions={[
                          <Button
                            key="edit"
                            size="small"
                            type="text"
                            icon={<EditOutlined />}
                            disabled={indexing}
                            onClick={() => {
                              setEditing(c);
                              setEditText(c.content);
                            }}
                          />,
                          <Popconfirm
                            key="del"
                            title="删除该切片？"
                            description="删除后不可恢复，检索将不再命中该块。"
                            onConfirm={() => delChunkMutation.mutate(c.id)}
                          >
                            <Button
                              size="small"
                              type="text"
                              danger
                              icon={<DeleteOutlined />}
                              disabled={indexing}
                            />
                          </Popconfirm>,
                        ]}
                      >
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 8,
                            }}
                          >
                            <Tag style={{ margin: 0, fontSize: 10 }}>#{c.seq + 1}</Tag>
                            {c.heading_path && (
                              <Typography.Text
                                type="secondary"
                                style={{ fontSize: 11 }}
                                ellipsis
                              >
                                {c.heading_path}
                              </Typography.Text>
                            )}
                            <span style={{ flex: 1 }} />
                            <Typography.Text
                              type="secondary"
                              style={{ fontSize: 10 }}
                            >
                              {c.token_count}t
                            </Typography.Text>
                            <Tag
                              color={c.embedded ? "success" : "warning"}
                              style={{ margin: 0, fontSize: 10 }}
                            >
                              {c.embedded ? "已向量化" : "无向量"}
                            </Tag>
                          </div>
                          <Typography.Paragraph
                            ellipsis={{ rows: 3, expandable: true }}
                            style={{ margin: "4px 0 0", fontSize: 12 }}
                          >
                            {c.content}
                          </Typography.Paragraph>
                        </div>
                      </List.Item>
                    )}
                  />
                  {hasMore && (
                    <div style={{ textAlign: "center", padding: 8 }}>
                      <Button
                        size="small"
                        onClick={() => loadChunks(chunks.length)}
                      >
                        加载更多切片
                      </Button>
                    </div>
                  )}
                </>
              ),
          },
        ]}
      />

      {/* 重切参数 */}
      <FormDrawer
        title="重新切分"
        open={rechunkOpen}
        onClose={() => setRechunkOpen(false)}
        onOk={() => rechunkMutation.mutate()}
        okText="开始重切"
        cancelText="取消"
        confirmLoading={rechunkMutation.isPending}
        width={420}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          基于既有解析产物重新切分并重新向量化（不重跑 docling 解析）。当前文档
          {doc.chunk_count} 块。
        </Typography.Paragraph>
        <Space direction="vertical" style={{ width: "100%" }} size={12}>
          <div>
            <Typography.Text style={{ fontSize: 12 }}>块大小（token）</Typography.Text>
            <InputNumber
              value={targetTokens}
              onChange={(v) => v !== null && setTargetTokens(v)}
              min={64}
              max={2048}
              style={{ width: "100%", marginTop: 4 }}
            />
          </div>
          <div>
            <Typography.Text style={{ fontSize: 12 }}>相邻块重叠（token）</Typography.Text>
            <InputNumber
              value={overlapTokens}
              onChange={(v) => v !== null && setOverlapTokens(v)}
              min={0}
              max={512}
              style={{ width: "100%", marginTop: 4 }}
            />
          </div>
        </Space>
      </FormDrawer>

      {/* 切片编辑 */}
      <FormDrawer
        title={`编辑切片 #${editing ? editing.seq + 1 : ""}`}
        open={editing !== null}
        onClose={() => setEditing(null)}
        onOk={() =>
          editing &&
          editText.trim() &&
          updateChunkMutation.mutate({ id: editing.id, content: editText.trim() })
        }
        okText="保存（并重新向量化）"
        cancelText="取消"
        confirmLoading={updateChunkMutation.isPending}
        width={560}
        okDisabled={!editText.trim()}
      >
        <Input.TextArea
          rows={10}
          value={editText}
          onChange={(e) => setEditText(e.target.value)}
          showCount
          maxLength={32000}
        />
      </FormDrawer>
    </Drawer>
  );
}
