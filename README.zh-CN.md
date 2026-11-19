# AI Photo Agent

**语言：** [English](README.md) | 简体中文

AI Photo Agent 是一款本地优先的 AI 摄影助手：上传照片后由视觉模型实时判断问题、
推荐风格并生成 Lightroom 参数，也可以把参数直接送入本机 Lightroom Classic 查看效果。

## 当前能力

1. 选择 JPG/JPEG、PNG、TIF/TIFF 或 Canon CR3 文件后自动上传并分析，不需要再点上传按钮。
2. CR3 使用相机内嵌 JPEG 预览做分析，原始 `.cr3` 不改动；TIFF 使用 Pillow 完整解码。
3. 默认调用 **DeepSeek 视觉分析**，根据当前照片生成：
   - 总体诊断；
   - 技术、构图、色彩/影调观察；
   - 恰好 3 个本次照片专属的风格和可执行参数。
4. UI 把排序第一的风格标为“AI 首选”并自动选中，也可以改选。
5. 生成 Pillow 近似预览、`lightroom_xmp_v2` 预设和 `lightroom_develop_v1` 显影参数。
6. 上传后在上传区立即展示照片；按 JPEG EXIF / CR3 CMT1 自动校正方向，预览对比图与上传图方向一致。
7. 通过 Lightroom Classic 插件写入 `AI Photo Agent - Before` 快照、应用参数并切换到修改照片模块。
8. 导出 XMP 时以附件响应下载。
9. Phase 2 参考图学习（后端 API 与插件工具）：上传最多 8 张参考图，生成构图/色彩/练习总结；该参考图分析流程仍使用 Mock（学习总结不依赖它）。网页「修图」界面已不再展示该板块，网页聚焦修图与知识库。
10. 长分析可走 `/api/photos/{id}/analyze/jobs` 后台任务，显示阶段进度并可取消（单进程最多 8 个 runner / 16 个活跃线程）。
11. 可把当前风格直接发送到 Lightroom、在 UI 内安装插件并查看桥接状态；全屏 Lightbox 支持以鼠标位置为锚点的滚轮缩放与拖动。
12. **Phase 2 学习总结与知识库**：每次分析成功都会自动生成学习总结——图像缩略图与元数据、分析快照、全部 3 个修图方向及参数总结、智能推荐强度（经自适应校验并渲染对应预览）、确定性的分类标签；知识库支持按 6 个维度标签/主分类筛选浏览、查看单张详情，并可通过 `/api/knowledge/*` 重建；照片重新分析会自动替换旧总结。页面分为「修图」与「知识库」两个顶层界面，顶部导航按钮可在两个界面间双向切换；DeepSeek Harness 插件面板的第二个 tab 同样是知识库视图。

## 在 DeepSeek Harness 中使用（插件）

`dsh-plugin/` 是接入 DeepSeek Harness 的持久化插件 `@local/dsh-photo-agent`：侧边栏「AI 修图」面板（修图 / 知识库 / 状态）和 5 个 `photo_agent_*` Agent 工具复用本仓库现有的 FastAPI 后端，不重写图像或模型逻辑。

安装到当前 Harness profile 后刷新页面即可使用；安装、工具、环境变量、验证与限制见
[AI 修图插件说明](dsh-plugin/README.md)。

## 图片分析与风格为什么不再固定

运行时默认 Provider 是 `deepseek`，模型是 `deepseek-flash`（支持图片输入）。每次上传都会
重新观察照片，返回不同的诊断文本、风格 id、名称、理由和参数，不会回退到固定模板。
缺少 API Key 时应用拒绝启动，也不会静默回退 Mock。

API Key 按顺序读取：

1. `AI_PHOTO_AGENT_DEEPSEEK_API_KEY`
2. `DEEPSEEK_API_KEY`
3. `AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE`，仅当你显式指向一个包含
   `DEEPSEEK_API_KEY: ...` 的 YAML 文件时生效

插件不会隐式读取其他应用的凭据库。本地开发可以复制 `.env.example` 为 `.env`，
由 `run_app.py` 加载；已经导出的环境变量优先。

Key 不会写入日志或 HTTP 响应。详细实现与实测见
[DeepSeek 实时图片诊断与动态风格](docs/DEEPSEEK_ANALYSIS.zh-CN.md)。

## 快速开始

需要 Python 3.12 或更新版本：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_app.py
```

服务默认运行在 `http://127.0.0.1:8000`，并自动打开浏览器。只启动服务：

```powershell
.\.venv\Scripts\python.exe run_app.py --no-browser --port 8000
```

## Lightroom 联动

项目内提供 Lightroom Classic 插件：

```text
lightroom/AIPhotoAgent.lrplugin/
```

首次使用：

1. 在 Lightroom 中打开 `文件 > 增效工具管理器 > 添加`；
2. 选择 `lightroom/AIPhotoAgent.lrplugin` 文件夹；
3. 重启 Lightroom；
4. 在 Web 端选中照片和风格，点击“发送到 Lightroom”；
5. 在 Lightroom 中选中目标照片，运行
   `图库 > 增效工具附加功能（部分版本为“附加信息”） > AI Photo Agent - 应用 Web 端待处理修图`。

插件会先写 `AI Photo Agent - Before` 快照，再应用参数并切换到修改照片模块。
当前 Lightroom Classic 15.1 + Windows 上 `createVirtualCopy` 会阻塞，所以虚拟副本模式
默认关闭（`USE_VIRTUAL_COPY=false`），修改直接落在选中照片上；可用 Before 快照或
Ctrl+Z 撤销。安装、真机验证记录和限制见
[Lightroom 修图插件说明](docs/LIGHTROOM_PLUGIN.zh-CN.md)。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_PHOTO_AGENT_ANALYSIS_PROVIDER` | `deepseek` | `deepseek` / `mock` / `openai` / `codex` |
| `AI_PHOTO_AGENT_DEEPSEEK_API_KEY` | 空 | DeepSeek Key（优先） |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek Key 回退 |
| `AI_PHOTO_AGENT_OPENAI_API_KEY` | 空 | OpenAI Key（优先） |
| `OPENAI_API_KEY` | 空 | OpenAI Key 回退 |
| `AI_PHOTO_AGENT_DEEPSEEK_MODEL` | `deepseek-flash` | DeepSeek 视觉模型 |
| `AI_PHOTO_AGENT_DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容地址 |
| `AI_PHOTO_AGENT_DEEPSEEK_TIMEOUT_SECONDS` | `180` | 单次分析超时 |
| `AI_PHOTO_AGENT_DEEPSEEK_MAX_RETRIES` | `1` | 429/5xx 重试次数（带退避与 `Retry-After`） |
| `AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE` | 空（禁用） | 可选的显式 YAML Key 文件 |
| `AI_PHOTO_AGENT_OPENAI_MODEL` | `gpt-5.6-terra` | `openai` Provider 使用的模型 |
| `AI_PHOTO_AGENT_OPENAI_TIMEOUT_SECONDS` | `45` | OpenAI 请求超时 |
| `AI_PHOTO_AGENT_OPENAI_MAX_RETRIES` | `2` | OpenAI SDK 重试次数 |
| `AI_PHOTO_AGENT_CODEX_MODEL` | `gpt-5.6-sol` | `codex` Provider 使用的模型 |
| `AI_PHOTO_AGENT_CODEX_EXECUTABLE` | 从 `PATH` 查找 | 显式指定 `codex` 可执行文件 |
| `AI_PHOTO_AGENT_CODEX_TIMEOUT_SECONDS` | `600` | Codex CLI 超时 |
| `AI_PHOTO_AGENT_DATA_DIR` | 仓库根目录 | 存储根目录；私有会话在 `<root>/data/`，上传在 `<root>/uploads/` |
| `AI_PHOTO_AGENT_LIGHTROOM_ROOT` | 自动探测 | Lightroom 用户目录 |
| `AI_PHOTO_AGENT_MAX_UPLOAD_BYTES` | `268435456`（256 MiB） | 上传字节上限 |
| `AI_PHOTO_AGENT_MAX_IMAGE_PIXELS` | `50000000` | 单图解码像素上限 |
| `AI_PHOTO_AGENT_SERVER_URL` | 由 `run_app.py` 发布 | 写入 Lightroom 桥接任务的回连地址 |

文件路径类配置（`DATA_DIR`、`LIGHTROOM_ROOT`、`CODEX_EXECUTABLE` 和可选凭据文件）会展开
`~`、Windows 的 `%VAR%` 以及 POSIX 的 `$VAR`。

离线复现/测试可显式设置：

```powershell
$env:AI_PHOTO_AGENT_ANALYSIS_PROVIDER = "mock"
```

## 安全与行为说明

- 带非 loopback `Origin` 的写请求返回 **403**；没有 `Origin` 头的请求（Lightroom Lua、命令行、`TestClient`）仍允许。这是最小 CSRF 防护，不是认证。
- 声明 `Content-Length` 超过 `max_upload_bytes + 1 MiB` 时在 **multipart 解析前返回 413**；后续分块读取上限仍然生效。
- `run_app.py` 在导入 app 之前把实际的 `--host/--port` 发布为 `AI_PHOTO_AGENT_SERVER_URL`，确保 Lightroom 桥接任务回连正确地址；显式设置的环境变量优先。
- 上传时把脱敏后的 basename 记录到 `PhotoSession.original_filename`，桥接任务在 `photo.original_filename` 中带上它（只含 basename，绝不含绝对路径）。
- DSH 插件的强度滑杆默认 `0.7`，并在自身路由上校验 Host/Origin/Sec-Fetch-Site；见 `dsh-plugin/README.md`。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -W error -p no:cacheprovider
```

当前结果：全量测试通过。本工作区的 `.pytest_cache` 存在 ACL 问题，必须加 `-p no:cacheprovider`。Lightroom 插件校验也通过：

```powershell
.\.venv\Scripts\python.exe scripts\validate_lightroom_plugin.py
```

历史浏览器实测记录见 [上传照片方向与 XMP 下载修复](docs/UPLOAD_ORIENTATION_XMP.zh-CN.md)：真实 CR3 上传/显示为 **4000×6000 纵向**，浏览器预览 1067×1600，XMP 以附件响应实际下载 2733 bytes。

真实 CR3 验证：上传 `D:\Photos\2026\2026-05-23\example.CR3` 后，分析接口返回 200，
生成 `urban-blue-clean`、`leafy-film-warm`、`high-key-minimal` 三个动态风格；第一套风格
的预览、XMP、显影参数接口均返回 200。结果保存在
`data/runtime/deepseek-live-analysis.json`。

## 文档

- [DeepSeek 实时图片诊断与动态风格](docs/DEEPSEEK_ANALYSIS.zh-CN.md)
- [上传照片方向与 XMP 下载修复](docs/UPLOAD_ORIENTATION_XMP.zh-CN.md)
- [Lightroom 修图插件与桥接说明](docs/LIGHTROOM_PLUGIN.zh-CN.md)
- [当前全部代码索引与架构说明](docs/CURRENT_CODEBASE_INDEX.zh-CN.md)
- [项目目标](docs/PROJECT_GOAL.zh-CN.md)
- [产品需求](docs/PRODUCT_REQUIREMENTS.zh-CN.md)
- [MVP 路线图](docs/MVP_ROADMAP.zh-CN.md)

## 已知限制

- DeepSeek 分析依赖网络、账户配额和模型输出质量，单次通常 20-60 秒。
- `deepseek` 模式下 Phase 2 参考图学习仍使用 Mock；新增的逐照片学习总结是确定性生成，不依赖该 Mock。
- 后台分析任务只存在于进程内：重启即丢，任务按 finished_at 保留 1 小时，没有跨重启队列。
- 上传仍会把整个文件读入内存；`Content-Length` 前置检查只信任声明值，公网部署仍需代理/ASGI body limit。
- 没有认证、账户、配额和删除/保留 API；非 loopback `Origin` 检查只是最小浏览器防护，不是完整 CSRF 边界。
- 虚拟副本模式尚未启用；Lightroom 真机只完成一次 RAW 参数应用，仍需逐项视觉核对。
- CR3 显示图仍是完整 4000×6000（约 5.5 MB）；大图加载会占用一点带宽。
- Web 预览是 Pillow 近似，不能替代 Lightroom 渲染。
