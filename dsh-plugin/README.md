# AI 修图（`@local/dsh-photo-agent`）

把本机 [AI Photo Agent](../README.zh-CN.md) 接入 DeepSeek Harness 的持久化插件。它在 Harness 侧边栏提供「AI 修图」面板，并把同一套能力暴露成 5 个 Agent 工具。

插件**不重写**照片分析逻辑：它管理并调用仓库现有的 FastAPI 后端（`.venv` + `run_app.py`），因此 CR3 内嵌预览、TIFF 解码、DeepSeek 视觉分析、Pillow 近似预览、XMP 与 Lightroom 桥接的行为和原 Web 应用完全一致。面板只是 React 视图，不解析图片、不调用模型。

## 交付形态

| 半边 | 内容 |
|---|---|
| Host | 后端探测/拉起/复用、5 个 `photo_agent_*` 工具、`/photo-agent/api` 本地接口、最近结果缓存 |
| Client | `sidebar.panellist` 图标 + `main` 面板：修图 / 知识库 / 状态 |
| 后端 | 现有 Python FastAPI 应用（默认 `http://127.0.0.1:8000`） |

```text
DSH Web GUI
  ├─ sidebar.panellist「AI 修图」
  └─ main 面板（React slot）
        │ 同源 fetch /photo-agent/api/*
        ▼
DSH Host 插件
  ├─ PhotoService（面板与工具共用）
  ├─ BackendManager（复用或拉起 run_app.py）
  ├─ StateStore（data/runtime/dsh-photo-agent/state.json）
  └─ 5 个 Agent 工具
        │ loopback HTTP
        ▼
现有 FastAPI 后端
  ├─ /api/photos · /api/lightroom · /api/learning · /api/knowledge
  ├─ /generated · /exports
  └─ Lightroom Bridge（预设目录 + inbox 队列）
```

## 安装

1. 确认本机已有仓库的 Python 环境：

   ```powershell
   .\.venv\Scripts\python.exe -c "import fastapi, uvicorn, PIL; print('ok')"
   ```

2. 在 Harness 中安装本目录（把 `C:\path\to\ai-photo-agent` 换成你的 checkout）：

   ```text
   plugin_manager install_bundle target "C:\path\to\ai-photo-agent\dsh-plugin"
   ```

   也可以让 Agent 调用 `plugin_manager` 完成；不要手改 profile 的 `package.json` 或 `cordis.patch.yml`。

3. 刷新 Harness 页面，左侧边栏底部会出现「AI 修图」图标。首次打开面板会探测/启动后端（通常 1–3 秒）。

4. 卸载：

   ```text
   plugin_manager remove_bundle target "@local/dsh-photo-agent"
   ```

   插件只新增工具、路由和缓存文件，不迁移或删除应用数据。

## 面板

### 修图

- 选择 JPG/JPEG、PNG、TIF/TIFF、Canon CR3 后**自动上传并分析**，上传期间显示已用时间。
- 展示总体诊断、技术/构图/色彩观察和恰好 3 套风格；第一套标记「AI 首选」并默认选中。
- 强度滑杆 0–100%（默认 70%）；生成 Pillow 近似预览（原图 vs 预览并排）。
- 上传照片、原图、近似预览和导出卡片中的预览图都可以点击进入全屏大图；滚轮缩放（以鼠标位置为锚点），放大后按住图片拖动平移，双击画面或「适应窗口」复位，Esc 或「关闭」退出。百分比按钮不恢复。
- 导出 XMP（附件下载）、生成 Lightroom 显影参数、发送到 Lightroom 队列。
- 安装/检查 Lightroom 插件、刷新 Lightroom 状态。
- 分析失败时保留照片 ID，可直接「重新分析」，不需要重新上传。

### 知识库

- 展示每次分析后自动生成的学习总结：缩略图、主分类、6 维标签、分析总结和 3 个修图方向的推荐强度。
- 顶部统计总数、覆盖主分类数和平均推荐强度；可按主分类或任一维度标签筛选，点「清除筛选」复原。
- 每张卡片可懒加载完整修图方向：风格名、推荐强度、推荐理由、参数总结、该强度的近似预览图与安全提示。
- 「为当前照片重新生成总结」重建插件最近一次分析照片的学习总结（未分析或摘要不存在时给出中文提示）。
- 学习总结与知识库数据来自本机后端 `/api/knowledge`；插件只做视图与代理，不重新计算分类或强度。

### 状态

- 后端地址、端口、Provider、Python 路径、数据目录、日志尾部。
- Lightroom 检测/注册状态、预设目录、桥接收件箱。
- 「打开完整界面」在浏览器打开现有 Web UI，作为兜底入口。
- 「清空当前结果」仅清空插件缓存。

## Agent 工具

| 工具 | 作用 |
|---|---|
| `photo_agent_analyze` | 上传本地照片或复用 `photo_id`，返回诊断、3 套风格与 `photo_id` |
| `photo_agent_preview` | 生成某套风格的 Pillow 近似预览，返回绝对文件路径与代理 URL |
| `photo_agent_export` | `action=xmp` 生成预设；`action=develop` 返回 Lightroom 显影参数 |
| `photo_agent_lightroom` | `status` 只读；`install` / `send` 写 Lightroom 目录，必须 `confirm:true` |
| `photo_agent_learning` | 学习主题的 `create` / `add_reference` / `analyze` / `get` |

工具与面板调用同一个 `PhotoService` 方法。工具成功后会把最近结果写入缓存，因此面板打开时能恢复上一次分析。

## 环境变量

在启动 Harness 的进程环境中设置：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DSH_PHOTO_AGENT_APP_DIR` | 本插件上一级（仓库根） | 含 `run_app.py` 的 checkout |
| `DSH_PHOTO_AGENT_PYTHON` | `<appDir>\.venv\Scripts\python.exe`，否则 `python` | 解释器 |
| `DSH_PHOTO_AGENT_PORT` | `8000` | 后端端口；与 Lightroom `Config.lua` / `Config.local.lua` 的 `SERVER_URL` 保持一致 |
| `DSH_PHOTO_AGENT_BASE_URL` | 空 | 只连接外部实例，不拉起进程；仅允许 loopback |
| `DSH_PHOTO_AGENT_AUTOSTART` | `true` | 首次使用是否自动拉起后端 |
| `DSH_PHOTO_AGENT_PROVIDER` | `deepseek` | 传给后端的 `AI_PHOTO_AGENT_ANALYSIS_PROVIDER` |
| `DSH_PHOTO_AGENT_DATA_DIR` | 空（后端默认仓库根） | 传给后端的 `AI_PHOTO_AGENT_DATA_DIR` |
| `DSH_PHOTO_AGENT_LIGHTROOM_ROOT` | 空（自动探测） | 传给后端，便于非默认安装/测试 |
| `DSH_PHOTO_AGENT_ANALYSIS_TIMEOUT_MS` | `240000` | 分析工具与 HTTP 请求超时 |
| `DSH_PHOTO_AGENT_START_TIMEOUT_MS` | `60000` | 等待后端就绪 |
| `DSH_PHOTO_AGENT_MAX_UPLOAD_BYTES` | `268435456` | 与后端 256 MiB 上限一致；16 位/多页 TIFF 常超过 128 MiB |

DeepSeek Key 由后端按顺序读取：`AI_PHOTO_AGENT_DEEPSEEK_API_KEY` → `DEEPSEEK_API_KEY` → 显式指定的 `AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE`。也可以把 Key 放进仓库根 `.env`（由 `run_app.py` 加载）。后端不再隐式读取其他应用的凭据库；插件不读取、不返回、不记录 Key。

## 安全与隐私

- 所有 `/photo-agent/api` 请求先校验 `Host` 必须是回环主机（`127.0.0.1` / `localhost` / `[::1]`，任意端口），`Origin` 存在时必须与 `Host` 一致，跨站（含 `Sec-Fetch-Site: cross-site/same-site`）请求拒绝；无 `Origin` 的本地工具调用仍可用。该规则覆盖 GET 与 POST。
- `/photo-agent/api/asset` 先对路径做有界多轮百分号解码，拒绝残留 `%`、反斜杠、点段与绝对 URL，再交给 WHATWG URL 解析器规范化，并只允许应用自己的 `generated` / `exports` / `display` / 学习参考图路径；知识库缩略图（`display`）与推荐强度预览（`generated/summaries`）同样走这条白名单；代理请求使用规范化后的 pathname，不能读任意本地文件。
- 照片只会发给后端当前配置的 AI Provider；插件不额外上传。分析通常 20–60 秒，取决于网络与账户配额。
- `photo_agent_lightroom` 的 `install` / `send` 必须 `confirm:true`；`send` 只写预设和队列，用户在 Lightroom 中执行菜单后才应用到照片。
- 后端日志只保留最近 200 行 stdout/stderr，不记录请求体或密钥。

## 验证

```powershell
$node = "C:\path\to\node.exe"
& $node scripts\check-client.mjs
& $node scripts\check-host.mjs
& $node scripts\smoke-backend.mjs
.\.venv\Scripts\python.exe -m pytest tests -q -W error -p no:cacheprovider
```

> 当前工作区的 `.pytest_cache` 目录存在 ACL 写入限制；加 `-p no:cacheprovider` 可以避免 pytest 在收尾阶段报 `PytestCacheWarning`，不影响测试结果。

- `check-client`：编译 client.js，断言 locale/样式与两个 Slot 注册（含放大查看器样式）。
- `check-host`：配置、资源白名单、错误映射、状态缓存、后端生命周期（fake spawn/fetch）。
- `smoke-backend`：真实 Python + mock Provider + 临时数据/Lightroom 目录，覆盖上传、分析、预览、XMP、develop、Lightroom 队列、学习模块、知识库总结/筛选/预览与代理资产；不触碰真实 Lightroom 目录。
- Python 全量回归通过（`pytest -q -W error -p no:cacheprovider`；测试数以仓库当前版本为准）。

### 2026-11-14 知识库面板

- 面板第二个 tab 由「摄影学习」改为「知识库」：展示后端自动生成的学习总结，支持主分类/标签筛选、方向懒加载、推荐强度预览与当前照片重建。
- 新增 Host 代理路由 `GET /knowledge/overview`、`GET /knowledge/summaries`、`GET /knowledge/summaries/:id`、`POST /knowledge/summaries/:id/rebuild`，服务层对应 `knowledgeOverview/knowledgeList/knowledgeGet/knowledgeRebuild`。
- Client bundle 版本戳更新为 `v2026-11-14.2`；`check-host` 增加知识库路由用例，`smoke-backend` 增加知识库总结/筛选/预览/重建检查。
- 验证：`node --check`、`check-client`、`check-host`、`smoke-backend`、Python `396 passed`。

### 2026-10-27 图像放大查看

- 新增通用 `ZoomableImage` 与全屏 `Lightbox`：上传照片、原图、近似预览、导出卡片预览图和摄影学习参考图均可点击放大。
- 验证：`node scripts/check-client.mjs` 通过（2 个 Slot 注册、2 个 effect，含 `.photo-zoomable` / `.photo-lightbox` 样式断言）；Python 回归与后端冒烟不受影响。

### 2026-10-28 滚轮缩放与拖动

- 用户要求恢复交互：滚轮放大/缩小，放大后鼠标拖动平移。
- 查看器实现：适应窗口初始值 `scale=1`，范围 1–8；滚轮以鼠标位置为锚点缩放；左键拖动平移并按可视区边界 clamp；双击或「适应窗口」复位；窗口 resize 复位。
- 仍不恢复 50%/100%/200%/400% 百分比按钮。
- Client bundle 版本戳更新为 `v2026-10-28.2`。
- 验证：`node --check`、`scripts/check-client.mjs`（含 wheel/pointer/reset 静态断言）、`check-host`、Python `154 passed`；服务端 bundle 可达并核对版本戳。
- 无浏览器控制，滚轮/拖动手感需用户刷新后人工确认；触摸双指捏合暂不支持。

### 2026-11-13 安全与生命周期加固

- 资源代理修复多重百分号编码绕过：有界解码直至稳定，残留 `%`、`\`、点段、绝对/协议相对 URL 一律拒绝；代理请求使用规范化后的 pathname。
- 路由 `Host`/`Origin` 校验覆盖全部方法；无 `Origin` 的本地调用仍按 `Host` 校验，跨站请求 403。
- `readBody` 在客户端 `aborted`/`close` 时以 400 `request-aborted` 结束，不再悬挂 Promise 或缓存请求体。
- 托管后端连接被拒时重新探测并重试一次（只重试“请求未送达”的情况，不会重放超时/已处理请求）；退出清理等待子进程真实退出，Windows 用 `taskkill /T /F` 兜底，避免孤儿后端。
- `photo_agent_*` 工具的 intensity 默认值统一为 `0.7`（70%），与面板默认一致。
- 验证：`node --check`、`check-client`（2 slot/2 effect）、`check-host`（新增资源/路由/中断/就绪恢复/退出回归用例）、`smoke-backend`（32 checks）。

### 2026-10-26 实测

- `install_bundle`：`application: applied`，`warnings: []`。
- Host 工具目录出现 5 个 `photo_agent_*` 工具；Client `sidebar.panellist` 出现 `photo-agent`（order 30），`main` 出现 `photo-agent` key。
- `check-client.mjs`：2 个 Slot 注册、2 个 effect；`check-host.mjs`：配置/资源/错误/缓存/生命周期通过；`smoke-backend.mjs`：**32 checks**（真实 Python + mock + 临时 Lightroom 根目录）。
- 真实会话：mock 分析小图与 **31 MB Canon CR3**（4000×6000，内嵌预览）均成功；panel 路由上传/分析/预览/导出/资产代理/学习全链路通过；跨站 POST 返回 403，`/asset` 目录穿越返回 403。
- 真实 DeepSeek：对合成渐变图执行一次 `provider=default` 分析成功，返回 3 套动态风格（`vivid_gradient_clean`、`soft_pastel_gradient`、`neon_nocturne`）。
- 验证产生的 session/上传/预览/导出/学习文件与插件缓存已清理；未向真实 Lightroom 队列发送测试任务。

## 故障排查

| 现象 | 处理 |
|---|---|
| 打开面板提示端口被占用 | 换 `DSH_PHOTO_AGENT_PORT`，并同步 `lightroom/AIPhotoAgent.lrplugin/Config.local.lua` 的 `SERVER_URL`；插件不会杀别人的进程 |
| 后端启动失败 | 看面板「状态」页日志尾部；检查 `.venv`、依赖、DeepSeek Key；也可设 `DSH_PHOTO_AGENT_PROVIDER=mock` 离线验证 |
| 后端未启动且 `AUTOSTART=false` | 先手动运行 `run_app.py --no-browser --port 8000`，或打开自动启动 |
| Lightroom 未检测到 | 检查 `DSH_PHOTO_AGENT_LIGHTROOM_ROOT` 或后端 `AI_PHOTO_AGENT_LIGHTROOM_ROOT`；仍可下载 XMP 手动导入 |
| 发送后 Lightroom 没变化 | 在 Lightroom 中选中目标照片，运行「图库 > 增效工具附加功能 > AI Photo Agent - 应用 Web 端待处理修图」 |
| 插件更新后未生效 | 面板副标题末尾的 `v...` 是 Client bundle 版本戳；看不到新版本戳说明仍在运行旧 bundle。请强制刷新（Ctrl+Shift+R），或完全关闭并重开 Harness 窗口/应用；仅切换面板不够 |

## 已知限制

- 面板视觉效果无法由自动测试验证，需要人眼确认；Slot 注册、client 编译、Host 接口与后端全链路均已自动验证。面板副标题带有 `vYYYY-MM-DD.N` 形式的 Client bundle 版本戳，可用来确认页面是否加载了新代码。
- 滚轮缩放与拖动依赖真实浏览器事件，无法在无浏览器控制的环境中自动验证；触摸双指捏合暂不支持。
- 插件依赖本机 Python 环境与仓库路径；移动/删除 checkout 会破坏默认配置。
- 分析延迟与结果质量来自现有 DeepSeek Provider；插件不缓存模型结果，只缓存最近一次展示数据。
- 插件卸载/HMR 会终止它自己拉起的后端；外部已有实例不受影响。DSH 异常退出后可能遗留后端进程，下次启动会复用。
- Lightroom 应用动作仍由用户在 Lightroom 中触发；插件不做自动点击。
