# Akashic Agent（前端复刻版）

这是 Akashic Agent 的前端复刻版，并附带一个只覆盖聊天主链路的 MVP 后端：保留原项目的 Dashboard、Chat、移动 Web、主题系统、插件面板、静态构建产物和前端测试；后端仅实现 WebSocket、内存 MessageBus 和 OpenAI-compatible 流式调用。

## 开发

```bash
npm install
npm run dev                 # Dashboard 开发服务器
npm run dev:chat            # Chat 开发服务器（MVP 主链路）
npm run build               # Dashboard、Chat、插件面板
npm run typecheck
```

聊天开发服务器使用根路径，`/chat` 和 `/settings` 可以直接访问：

```text
http://127.0.0.1:5173/
http://127.0.0.1:5173/chat
http://127.0.0.1:5173/settings
```

生产构建仍使用 `/assets/` 作为静态资源前缀；启动后端时，构建产物由
`/chat`、`/settings`、`/dashboard` 页面路由和 `/assets` 静态挂载提供。

Vite 开发代理默认指向 `http://127.0.0.1:2236`，WebSocket 指向同一端口。后端启动方式和模型配置见 [`backend/README.md`](/E:/Project/akashic-agent-mine/backend/README.md)。

## 目录

- `frontend/dashboard`：桌面 Dashboard
- `frontend/chat`：桌面 Chat、移动 Web、设置和运行目录 UI
- `frontend/theme`：主题运行时和 Material 组件
- `plugins`、`plugin_packages`：前端插件面板资源（仅保留 TypeScript/JavaScript/CSS/TOML，插件 Python 实现已移除）
- `static`：原项目已构建的静态资源，可在没有 Node 构建环境时直接作为参考
- `backend-contract.md`：后端重写所需的 REST/WebSocket 接口契约

## 后端边界

当前 MVP 保存基础 session/turn/message 记录，但不拼接上下文；暂不实现 Dashboard、插件运行时、调度器和 MCP。模型配置读取沿用复制的 SQLite 注册表，并支持环境变量兜底。完整接口字段仍以 `backend-contract.md` 为参考。
