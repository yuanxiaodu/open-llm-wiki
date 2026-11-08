// defineConfig 取自 vitest/config 而不是 vite：下面那个 test 键只有它的类型认
// （vite 自己的 UserConfig 里没有 test，写了 tsc 直接红）。它就是把 vite 的同名函数
// 再导出一遍，所以 `vite build` / `vite dev` 的行为一个字都没变。
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 构建产物固定落在 web/dist：FastAPI（app/main.py）就是按这个路径挂 /assets
// 并对非 /api 路径回退 index.html 的，改了这里等于把托管链路一起改。
// dev 时前端跑在 vite 自己的端口上，/api 代理回本机的 uvicorn，
// 这样 fetch 仍是同源、cookie 照常带上（裁定 R11 的 JSON 契约不受影响）。
// 原来这里还代理了一条 /static：那是 S1 旧面的资产目录，S3 T6a 已整个删掉
// （app/static 与 main.py 的挂载一起退场），代理留着只会指向一个不存在的路径。
const WEB_ROOT = new URL("./src", import.meta.url).pathname;
const REPO_ROOT = new URL("..", import.meta.url).pathname;

export default defineConfig({
  plugins: [react()],
  resolve: {
    // 插件前端放在 web/ 之外的 plugins/<id>/web/，用 @app/* 引用核心模块。
    alias: { "@app": WEB_ROOT },
  },
  build: {
    outDir: "dist",
  },
  server: {
    fs: {
      // dev server 需要读仓库根的 plugins/。
      allow: [REPO_ROOT],
    },
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  test: {
    // CSS 要真处理，不许 vitest 拿空串顶掉（它的默认 css:false）。
    // src/shell.test.ts 用 `theme.css?raw` 把窄屏抽屉那几条 visibility 规则读进来钉住，
    // 拿到空串的话那些钉子会**永远绿**——比没有钉子更坏。
    css: { include: [/\.css/] },
    // 核心测试 + 插件自带测试（插件在 web/ 之外，构建期被 @app 别名与 glob 发现）。
    include: ["src/**/*.test.{ts,tsx}", "../plugins/**/*.test.{ts,tsx}"],
  },
});
