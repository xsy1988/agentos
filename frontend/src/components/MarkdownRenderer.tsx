/**
 * Markdown 渲染组件：消息、SKILL.md、记忆三处复用。
 * react-markdown + GFM + 代码高亮 + KaTeX 数学公式。
 */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";

export default function MarkdownRenderer({ content }: { content: string }) {
  return (
    <div className="markdown-body" style={{ fontSize: 14, lineHeight: 1.6 }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight, rehypeKatex]}
        components={{
          // 表格优化
          table: ({ children }) => (
            <table
              style={{
                borderCollapse: "collapse",
                width: "100%",
                margin: "0.5em 0",
                fontSize: "0.9em",
              }}
            >
              {children}
            </table>
          ),
          th: ({ children }) => (
            <th
              style={{
                border: "1px solid rgba(128,128,128,0.3)",
                padding: "4px 8px",
                textAlign: "left",
                background: "rgba(128,128,128,0.08)",
              }}
            >
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td
              style={{
                border: "1px solid rgba(128,128,128,0.2)",
                padding: "4px 8px",
              }}
            >
              {children}
            </td>
          ),
          // 内联代码用等宽
          code: ({ className, children, ...props }) => {
            const isBlock = className?.includes("language-");
            if (isBlock) {
              return (
                <code className={className} {...props}>
                  {children}
                </code>
              );
            }
            return (
              <code
                className="font-mono-tight"
                style={{
                  background: "rgba(128,128,128,0.15)",
                  padding: "0.1em 0.3em",
                  borderRadius: 3,
                }}
                {...props}
              >
                {children}
              </code>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
