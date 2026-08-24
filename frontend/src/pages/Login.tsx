/**
 * 登录/初始化引导页（前端设计 §4 空状态）。
 * 首次使用：初始化唯一账号 → 已有：登录。
 */
import { useState, useEffect } from "react";
import { Card, Form, Input, Button, Tabs, Typography, Alert } from "antd";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/store/auth";
import { api } from "@/api/client";

export default function LoginPage() {
  const navigate = useNavigate();
  const { init, login } = useAuthStore();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"login" | "init">("login");

  const [initForm] = Form.useForm();
  const [loginForm] = Form.useForm();

  // 探测是否已初始化：有 token 则尝试 /auth/me，成功跳转首页
  useEffect(() => {
    const token = localStorage.getItem("token");
    if (!token) return;
    api.get("/auth/me")
      .then(() => navigate("/"))
      .catch(() => {/* token 无效，留在登录页 */});
  }, [navigate]);

  const handleLogin = async (values: { username: string; password: string }) => {
    setLoading(true);
    setError(null);
    try {
      await login(values.username, values.password);
      navigate("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败");
    } finally {
      setLoading(false);
    }
  };

  const handleInit = async (values: {
    username: string;
    password: string;
    display_name: string;
  }) => {
    setLoading(true);
    setError(null);
    try {
      await init(values.username, values.password, values.display_name);
      navigate("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : "初始化失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "var(--ant-color-bg-container)",
      }}
    >
      <Card style={{ width: 420 }} styles={{ body: { padding: 32 } }}>
        <Typography.Title level={3} style={{ textAlign: "center", marginBottom: 24 }}>
          Agent 平台
        </Typography.Title>

        {error && (
          <Alert
            type="error"
            message={error}
            style={{ marginBottom: 16 }}
            closable
            onClose={() => setError(null)}
          />
        )}

        <Tabs
          activeKey={mode}
          onChange={(k) => setMode(k as "login" | "init")}
          items={[
            {
              key: "login",
              label: "登录",
              children: (
                <Form form={loginForm} layout="vertical" onFinish={handleLogin}>
                  <Form.Item label="用户名" name="username" rules={[{ required: true }]}>
                    <Input />
                  </Form.Item>
                  <Form.Item label="密码" name="password" rules={[{ required: true }]}>
                    <Input.Password />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" loading={loading} block>
                    登录
                  </Button>
                </Form>
              ),
            },
            {
              key: "init",
              label: "初始化",
              children: (
                <Form form={initForm} layout="vertical" onFinish={handleInit}>
                  <Form.Item
                    label="显示名称"
                    name="display_name"
                    rules={[{ required: true, min: 1, max: 64 }]}
                  >
                    <Input />
                  </Form.Item>
                  <Form.Item
                    label="用户名"
                    name="username"
                    rules={[
                      { required: true, min: 2, max: 64 },
                      { pattern: /^[a-zA-Z0-9_-]+$/, message: "仅允许字母、数字、下划线和短横线" },
                    ]}
                  >
                    <Input />
                  </Form.Item>
                  <Form.Item
                    label="密码"
                    name="password"
                    rules={[{ required: true, min: 8, max: 128 }]}
                  >
                    <Input.Password />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" loading={loading} block>
                    创建账号
                  </Button>
                </Form>
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
