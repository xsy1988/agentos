/**
 * 通用右侧表单抽屉（前端设计 §4 右侧 Drawer 规范）。
 *
 * 镜像 Antd Modal 的 onOk/okText/cancelText/confirmLoading API，便于把
 * 「编辑/查看/创建内容」类弹窗直接替换为右侧 Drawer（含单字段输入）。
 * 纯确认类（构建版本/审核意见/删除 Popconfirm/图片外发）与 ⌘K 命令面板不走此组件。
 */
import type { ReactNode } from "react";
import { Button, Drawer, Space } from "antd";

export interface FormDrawerProps {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  onOk: () => void;
  okText?: string;
  cancelText?: string;
  confirmLoading?: boolean;
  /** 确认按钮禁用（如必填项为空）；对应原 Modal 的 okButtonProps.disabled */
  okDisabled?: boolean;
  width?: number;
  destroyOnClose?: boolean;
  children?: ReactNode;
}

export default function FormDrawer({
  open,
  title,
  onClose,
  onOk,
  okText = "保存",
  cancelText = "取消",
  confirmLoading,
  okDisabled,
  width = 520,
  destroyOnClose,
  children,
}: FormDrawerProps) {
  return (
    <Drawer
      placement="right"
      width={width}
      open={open}
      onClose={onClose}
      destroyOnClose={destroyOnClose}
      title={title}
      footer={
        <Space style={{ display: "flex", justifyContent: "flex-end" }}>
          <Button onClick={onClose}>{cancelText}</Button>
          <Button
            type="primary"
            loading={confirmLoading}
            disabled={okDisabled}
            onClick={onOk}
          >
            {okText}
          </Button>
        </Space>
      }
    >
      {children}
    </Drawer>
  );
}
