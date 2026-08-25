/**
 * Markdown 渲染组件：消息、SKILL.md、记忆三处复用。
 * react-markdown + GFM + 代码高亮 + KaTeX 数学公式。
 * 排版样式在 global.css 的 .markdown-body（GitHub Primer 为蓝本，
 * antd CSS 变量驱动暗/亮自适应）；代码块带语言标签 + 复制按钮。
 */
import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import { CheckOutlined, CopyOutlined } from "@ant-design/icons";

// 提取 React 子树的纯文本（复制按钮用；高亮代码是嵌套 span 结构）
function nodeText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (typeof node === "object" && "props" in node) {
    return nodeText((node as React.ReactElement<{ children?: ReactNode }>).props.children);
  }
  return "";
}

/** 代码块：头部条（语言标签 + 复制按钮）+ 深色代码区。 */
function CodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const codeEl = (Array.isArray(children) ? children[0] : children) as
    | React.ReactElement<{ className?: string }>
    | undefined;
  const className = codeEl?.props?.className || "";
  const lang = /language-(\w+)/.exec(className)?.[1] || "text";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(nodeText(children));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard 不可用（非安全上下文）时静默
    }
  };

  return (
    <div className="md-code-wrap">
      <div className="md-code-head">
        <span>{lang}</span>
        <button
          type="button"
          className={`md-code-copy${copied ? " copied" : ""}`}
          onClick={copy}
        >
          {copied ? <CheckOutlined /> : <CopyOutlined />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

export default function MarkdownRenderer({ content }: { content: string }) {
  return (
    <div className="markdown-body" style={{ fontSize: 14, lineHeight: 1.6 }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight, rehypeKatex]}
        components={{
          // 代码块：头部条 + 复制按钮（样式在 .md-code-wrap）
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          // 表格：圆角边框容器 + 横向滚动（窄气泡内长表不撑破布局）
          table: ({ children }) => (
            <div className="md-table-wrap">
              <div className="md-table-scroll">
                <table>{children}</table>
              </div>
            </div>
          ),
          // 行内代码：等宽字体类 + CSS 胶囊样式；块级代码样式由 .md-code-wrap 接管
          code: ({ className, children, ...props }) => {
            if (className?.includes("language-")) {
              return (
                <code className={className} {...props}>
                  {children}
                </code>
              );
            }
            return (
              <code className="font-mono-tight" {...props}>
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
