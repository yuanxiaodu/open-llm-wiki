import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";
import "./theme.css"; // 方向 B 对话令牌与基础件（按钮/输入/横幅/卡片/胶囊）
import "./vault.css"; // vault 界面令牌（只挂 .vault 子树）与只读 wiki 样式

const container = document.getElementById("root");
if (!container) {
  throw new Error("index.html 里缺了 #root");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
