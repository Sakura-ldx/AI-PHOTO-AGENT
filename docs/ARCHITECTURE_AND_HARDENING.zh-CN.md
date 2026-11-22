# 架构与工程加固约定（修图 / 知识库）

本文记录修图、学习总结、知识库和系列一致性的代码边界、可靠性约定与质量门禁。
目标是让新代码有明确的落点，而不是继续把校验、搜索和文件生命周期堆进路由或工作流函数。

## 分层

```
HTTP 适配层        app/api/*.py, app/main.py
    ↓
应用服务层         app/services/workflows.py
                   app/services/analysis_tasks.py（后台分析生命周期）
                   app/services/knowledge_query.py
                   app/services/series.py（系列匹配编排与签名缓存）
    ↓
领域规则层         app/services/learning_summary.py
                   app/services/series.py 中的纯相似度函数
                   app/schemas.py, app/knowledge_schemas.py
    ↓
持久化 / 外部适配   app/services/storage.py
                   app/adapters/*.py
```

- **HTTP 适配层**只做参数解析、调用服务、组装响应。路由函数不实现搜索、facet 聚合或分页。
- **应用服务层**编排一次用例：读取状态、调用纯函数、渲染派生资源、持久化。它负责事务边界和失败策略。
- **领域规则层**保持确定性、无 I/O，便于直接单元测试。`classify_analysis`、`recommend_intensity`、`dhash64`、`build_page` 都属于这一层。
- **持久化层**负责路径校验、原子写、锁和损坏数据策略。任何 ID 在拼路径前都必须通过 `RESOURCE_ID_PATTERN`。

## 知识库读取路径

```
GET /api/knowledge/summaries
  -> KnowledgeQuery.from_params(...)       # 参数规范化与 400 校验
  -> storage.list_knowledge_summaries()    # 读取并校验磁盘记录
  -> build_page(summaries, query)          # 全量 facets + 过滤 + 排序 + 分页
  -> KnowledgeSummaryListResponse
```

`app/services/knowledge_query.py` 不依赖 FastAPI 或文件系统。搜索、facet、分类计数、风格计数和分页都在这里，测试可直接构造普通对象。

## 已落实的可靠性 / 安全不变量

1. **派生数据失败即关闭。** 知识总结是可重建的派生数据，但磁盘记录一旦存在就必须通过完整 Pydantic 校验。损坏记录返回通用 500，不把恶意字段投影给浏览器。
2. **URL 只允许同源绝对路径。** `LocalUrl` 拒绝 `javascript:`、`//host`、反斜杠、空白和控制字符。前端 `sameOriginUrl` 只是第二道防线。
3. **文件名单行化。** `KnowledgeImage.original_filename` 使用 `SingleLineText`；系列提示词中的文件名、风格名和摘要还会再次折叠为单行，避免嵌入数据伪造新的提示词行。
4. **时间统一为 UTC。** `UTCDateTime` 把历史 naive 时间按 UTC 解释，避免排序和 `max()` 混用 aware/naive 时抛 `TypeError`。
5. **重建不破坏旧结果。** 学习总结预览先 `ensure_knowledge_previews`（只建目录），新总结成功保存后才 `prune_knowledge_previews` 清理旧文件。渲染失败时旧总结和旧预览仍然可读。
6. **原子写。** 会话、总结、签名缓存都先写唯一临时文件、`fsync`，再 `os.replace`。签名缓存还限制单文件大小并校验数值范围，损坏时忽略并重算。
7. **系列匹配是增强项。** `build_series_state` / `apply_series_analysis` 的异常只记录警告，不阻断照片分析。缓存使用失败不致命。
8. **降级不冒充已验证。** 参数解析器返回 `report=None` 时，预览/导出/学习方向都标记 `verified=False` 并附带降级警告，不再把 fallback 当作已验证结果。
9. **浏览器侧有明确的安全头。** 除 `X-Content-Type-Options`、`X-Frame-Options` 等基础头外，应用返回 CSP：默认只允许同源，禁止 object/base/frame 嵌入，脚本和样式只允许自身及内联块。新增外部资源时必须同步修改策略，而不是直接放开 `*`。
10. **后台分析任务可收尾。** 注册表限制并发数和在途数量；`shutdown()` 会把活动任务标记为 `cancelled` 并释放 runner 槽位，迟到的 provider 结果不能覆盖已取消状态。
11. **一次用例只测量一次。** 学习总结构建把已有 `measurements` 传入参数解析器；只有在没有现成测量时才调用 `measure_file`，避免每个候选强度重复解码原图。

## 数据流

```
上传 -> storage 保存原图/展示图/会话
分析 -> 测量 -> 系列匹配（可选）-> provider -> 系列约束应用 -> 保存分析
     -> save_analysis 清理旧总结/旧预览（分析变化时）
学习总结 -> 读取分析 -> classify_analysis
         -> 3 个方向：recommend_intensity -> resolver -> render preview
         -> save_knowledge_summary -> prune stale previews
```

## 测试与质量门禁

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -W error -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check app tests scripts
```

- `pyproject.toml` 固定 Ruff 的 Python 目标版本、排除目录和规则集；只启用会影响正确性、导入、异常边界和现代语法的规则，不把格式化规则混进门禁。
- `requirements-dev.txt` 固定 Ruff 版本，CI 在安装后先跑 lint，再跑隐私扫描、pytest 和 Lightroom 插件校验。新增规则时先清理基线，再一次性打开。

新增功能时至少覆盖：

- 参数边界（400/422）与损坏持久化记录的 500 行为；
- 失败重建后旧资源和旧 JSON 仍可读；
- 路径穿越、控制字符、恶意 URL 的拒绝；
- 领域纯函数的确定性（分类、强度、搜索、分页）；
- 后台任务取消/关闭后状态不会被迟到结果覆盖。

## 当前已知取舍

- 知识库每次请求都读取全部总结。照片规模较小时这是最简单且一致的做法；规模上升后可在 `knowledge_query` 前增加索引缓存，而不改 API 契约。
- 系列匹配会读取全部会话再截断到 50 个候选。后续可在存储层提供按时间排序的轻量列表，避免解析无关会话。
- 进程内锁只覆盖单进程。多进程部署需要文件锁或单写者服务；当前定位仍是本机单用户应用。
- 上传会先整体读入内存再落盘，CR3 预览提取也会读取整个 RAW 文件。当前上限是 256 MiB，峰值内存可能更高；大文件场景应改为流式落盘和分段扫描。
- 没有身份认证；非回环 `Origin` 拒绝只是浏览器侧最低限度防护，公网部署必须加反向代理和认证。
