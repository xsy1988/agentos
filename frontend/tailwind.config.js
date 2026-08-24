/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: [
          "JetBrains Mono",
          "Fira Code",
          "SF Mono",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
  // 不使用 Preflight 以避免与 Ant Design 样式冲突
  corePlugins: {
    preflight: false,
  },
};
