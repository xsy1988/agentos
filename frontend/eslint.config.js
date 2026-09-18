/**
 * 前端 ESLint（flat config，ESLint 9）。此前 `npm run lint` 指向一个不存在的
 * eslint —— 门是假的比没有更坏，故补齐依赖与配置，让 lint 真能跑。
 *
 * 规则取向：只开「能抓住真 bug」的项（未用变量、hooks 依赖、refresh 边界），
 * 不引入格式化规则（格式由 tsc/build 与人工评审把关，避免与既有风格打架）。
 */
import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "*.config.js", "vite.config.d.ts"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // 类型已由 tsc 把关；unused 交给编译器报错更准（含 type-only 引用场景）
      "@typescript-eslint/no-unused-vars": "off",
      "no-unused-vars": "off",
      // 后端返回的是 JSON，事件/卡片载荷天然是 unknown 记录，逐层断言成本高于收益
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
);
