/**
 * 「复制引用」按钮（Runs / Chat 共用）：一键把 run / 产物的引用串写入剪贴板。
 * 复制实现与兜底见 utils/clipboard.ts，提示沿用 antd message 约定。
 */
import { useState } from "react";
import { Button, Tooltip, message as antdMessage } from "antd";
import { CopyOutlined } from "@ant-design/icons";
import { copyText } from "@/utils/clipboard";

export default function CopyRefButton({
  text,
  tooltip = "复制引用",
  withText = false,
}: {
  text: string;
  tooltip?: string;
  /** 是否带文字（列表/卡片用图标，详情工具栏用文字） */
  withText?: boolean;
}) {
  const [busy, setBusy] = useState(false);

  const onCopy = async (e: React.MouseEvent) => {
    // 卡片/行本身可点（跳转 run），复制不能连带触发
    e.stopPropagation();
    setBusy(true);
    const ok = await copyText(text);
    setBusy(false);
    if (ok) antdMessage.success("引用已复制");
    else antdMessage.error("复制失败，请手动选择复制");
  };

  return (
    <Tooltip title={tooltip}>
      <Button
        type="text"
        size="small"
        icon={<CopyOutlined />}
        loading={busy}
        onClick={onCopy}
      >
        {withText ? "复制引用" : null}
      </Button>
    </Tooltip>
  );
}
