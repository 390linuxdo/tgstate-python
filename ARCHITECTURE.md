# ARCHITECTURE

## 概述
`tgState V2` 是一个基于 FastAPI + Telegram Bot + SQLite 的私有文件存储/图床服务。  
通过一个 Telegram 机器人把文件存进指定频道（利用 Telegram 的云存储），同时提供一个 Web 前端、API，以及 SSE 事件推送，实现上传、下载、分享、删除、批量管理等功能。

核心功能：
- Web UI：上传、浏览、删除文件，查看图片图床视图，自助更新访问密码。
- REST API：上传 (`/api/upload`)、下载 (`/d/...`)、列举 (`/api/files`)、删除、批量删除、设置密码。
- Telegram 机器人：监听频道消息，将新文件写入数据库、处理 “get” 回复生成下载链接、同步删除。
- 存储：SQLite 保存元数据（文件名、复合 file_id、大小、上传时间），实际文件托管在 Telegram 频道。
- 实时推送：SSE (`/api/file-updates`) 将新增/删除事件推送给前端。

## 目录结构
```
app/
├─ api/routes.py           # FastAPI API 路由（上传、下载、SSE、管理）
├─ bot_handler.py          # python-telegram-bot handlers & Application builder
├─ core/
│   ├─ config.py           # Pydantic Settings + .password 覆盖逻辑
│   └─ http_client.py      # FastAPI lifespan，建库、共享 httpx、启动/停止 Bot
├─ database.py             # SQLite 访问层（初始化、增删查）
├─ events.py               # asyncio.Queue 事件总线
├─ main.py                 # FastAPI 入口，挂载中间件、静态资源、路由
├─ pages.py                # UI Router（主页/图床/设置/密码页面）
├─ services/telegram_service.py  # Telegram Bot API 的上传/下载/删除逻辑
├─ static/                 # CSS / JS
└─ templates/              # Jinja2 模板

.github/workflows/         # Docker build CI
.env.example               # 环境变量示例
Dockerfile                 # 运行镜像
requirements.txt           # Python 依赖
README.md                  # 项目说明
```

## 模块职责
### `app/main.py`
- FastAPI 应用实例，挂载 `lifespan`（统一初始化/清理逻辑）。
- 全局 HTTP 中间件：基于 `.password` 或环境变量实现页面访问控制。
- 挂载静态文件目录、模板、注册 API 和页面路由。

### `app/core/http_client.py`
- `lifespan(app)`：启动时初始化数据库、共享 `httpx.AsyncClient`、调用 `create_bot_app` 并开启 `python-telegram-bot` 的轮询；关闭时依次清理。
- `get_http_client()`：FastAPI 依赖，提供共用 httpx 客户端（用于下载 Telegram 文件、流式传输等）。

### `app/core/config.py`
- `Settings`：通过 `pydantic-settings` 加载 `BOT_TOKEN`, `CHANNEL_NAME`, `PASS_WORD`, `PICGO_API_KEY`, `BASE_URL` 等。
- `get_settings()`：缓存的配置获取函数。
- `get_active_password()`：优先读取 `.password`，否则回退到环境变量 `PASS_WORD`，供中间件和上传验证使用。

### `app/bot_handler.py`
- `handle_new_file`：监听频道消息，筛选合法 chat，提取文件/图片，生成复合 ID (`message_id:file_id`)，存入数据库，并向 `file_update_queue` 推送 “add” 事件。
- `handle_get_reply`：当用户回复消息输入 `get` 时，解析清单文件（manifest）和原始文件名，生成下载链接并回复。
- `handle_deleted_message`：检测消息删除，通过 `database.delete_file_by_message_id` 移除本地记录，并推送 “delete” 事件。
- `create_bot_app()`：依据 `BOT_TOKEN` 创建 `python-telegram-bot` Application，注册上述 handlers，并在 `lifespan` 中启动。

### `app/services/telegram_service.py`
- 构造器：基于 `HTTPXRequest` 设置高超时时间，初始化 `telegram.Bot`。
- `_upload_as_chunks` / `_upload_chunk`：将大文件按 19.5 MB 分块上传，通过清单（manifest）聚合，记录复合 ID。
- `upload_file()`：根据文件大小选择直接上传或分块上传，成功后写数据库。
- `get_download_url()`：调 Telegram API 取 `file_path`，用于后续 HTTP 下载。
- `delete_message()` / `delete_file_with_chunks()`：完全删除单个文件或清单及其所有分块，处理 “消息未找到” 情况。
- `list_files_in_channel()`：遍历频道历史列出文件，区分单文件/清单。
- `get_telegram_service()`：缓存的 FastAPI 依赖，供 API/页面调用。

### `app/api/routes.py`
- `/api/upload`：接受 `UploadFile`，校验 API Key / 登录 cookie；临时保存后调用 `TelegramService.upload_file`，返回 `/d/...` 链接。
- `/d/{file_id}/{filename}`：下载路由，自动识别清单或单文件；清单使用 `stream_chunks` （逐块下载并串流输出），单文件直接透传；设置 `Content-Disposition`/`Content-Type`。
- `/api/file-updates`：SSE 端点，从 `file_update_queue` 流式输出 JSON。
- `/api/files`：读取 `database.get_all_files()` 返回文件列表。
- `/api/files/{file_id}`：调用 `telegram_service.delete_file_with_chunks()` 并同步删除本地记录。
- `/api/set-password`：写 `.password` 文件，实现运行时修改密码。
- `/api/batch_delete`：对一组 file_id 执行删除逻辑。
- `stream_chunks(...)`：辅助生成器，实时获取每个分块的下载 URL 并流式传输。

### `app/database.py`
- `init_db()`：创建 `files` 表（filename, file_id, filesize, upload_date）。
- `add_file_metadata()` / `get_all_files()` / `get_file_by_id()` / `delete_file_metadata()` / `delete_file_by_message_id()`：封装 SQLite CRUD，所有操作都配合线程锁保证安全。

### `app/pages.py`
- 定义 Web UI 路由：
  - `/`：主页，显示文件列表、上传控件。
  - `/settings`：修改密码页面。
  - `/pwd`：密码输入。
  - `/image_hosting`：筛选图片文件的图床视图。
  - `/share/{file_id}`：分享页（生成 HTML/Markdown 链接）。
- 模板由 `app/templates/*.html` 和 `app/static/*` 提供界面。

### 其他
- `app/events.py`：全局 `asyncio.Queue` (`file_update_queue`)，Bot 生产、SSE 消费，实现实时同步。
- `.env.example`：说明所有环境变量。
- `Dockerfile`：`python:3.11-slim` 镜像，复制 `app/`，执行 `uvicorn app.main:app`。
- `.github/workflows/docker-image.yml`：CI 用于构建并推送 Docker 镜像。

## 机器人启动流程
1. **入口**：`uvicorn app.main:app`（Dockerfile）或本地命令 `uvicorn app.main:app --reload`。
2. **`FastAPI` 初始化**（`app/main.py:13`）：
   - 注册 `lifespan=app.core.http_client.lifespan`。
   - 应用实例创建后立即进入 lifespan 上下文。
3. **`lifespan` 启动阶段**（`app/core/http_client.py:13`）：
   1. 调 `database.init_db()` 建立 `files` 表。
   2. 创建共享 `httpx.AsyncClient`（高并发/超时配置），存入模块级变量。
   3. 调 `create_bot_app()`（`app/bot_handler.py:146`）：
      - `get_settings()` 读取 `BOT_TOKEN` 等。
      - `python-telegram-bot` Application 注册 `handle_new_file`、`handle_get_reply`、`handle_deleted_message`。
      - `Application.initialize()` → `start()` → `updater.start_polling()`，开始后台轮询。
   4. 当 `lifespan` `yield` 时，FastAPI 服务开始处理 HTTP 请求。
4. **运行时**：
   - HTTP 路由和页面依赖 `get_telegram_service()` / `get_http_client()` 使用同一 T
elegram Bot及 httpx 实例。
   - Bot Handler 和 API 共享 `database`、`file_update_queue`，实现联动。
5. **关闭流程**（`lifespan` 退出）：
   - 关闭共享 `httpx.AsyncClient`。
   - `updater.stop()` → `bot_app.stop()` → `bot_app.shutdown()`，优雅停止 Telegram 轮询。
##  Bot зƬϴ
- ã `BOT_TOKEN`/`CHANNEL_NAME` Ϊ botͨ `EXTRA_BOTS`JSON ַ׷Ӹ `{"name","token","channel_name"}` ϣ`MULTIBOT_THRESHOLD_MB`Ĭ 10MBʱòС
- ϴ`TelegramService`  `upload_file` аļС bot ѡԡʱԼ 8MB п鲢ѯͬ bot ϴȫɺ bot ϴ manifest ļ `manifest_data` д SQLite
- أ`/d/{file_id}/{filename}` Ȳѯݿ⣬`strategy=multi_bot` ļ¼ʹ `stream_multi_bot_chunks`  manifest ԪƬȡӣ˳ɰ manifest ԭ߼
- 嵥Ϣmanifest ıݾɸʽϴɺ caption `[MULTIPART UPLOAD COMPLETED]` ժҪֱӷʵ URL
