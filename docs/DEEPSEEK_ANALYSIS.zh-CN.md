# DeepSeek 实时图片诊断与动态风格

> 目标：上传照片后，诊断和 3 个风格不再来自固定模板，而是由视觉模型根据当前照片判断。  
> 记录时间：2026-10-24（Asia/Shanghai）

## 1. 结论

照片分析默认 Provider 已从 `mock` 切换为 `deepseek`。默认模型为
`deepseek-flash`，通过 DeepSeek 的 OpenAI 兼容 Chat Completions API 接收图片输入，
返回：

- 针对当前照片的总体诊断；
- 技术、构图、色彩/影调观察；
- 恰好 3 个**本次照片专属**的编辑风格，包含名称、理由和完整可执行参数；
  排序第一标为“AI 首选”并自动选中，用户仍可改选其他风格；
- 黑白只在画面确实受益时出现，不再作为每张图固定的第三套；
- 分析前会检索同系列/近似重复照片，命中时锁定或对齐风格，保证一组照片的方向一致。

修复前，Mock 永远返回“自然纪实 / 电影冷暖 / 高反差黑白”；修复后，同一套服务端代码
会因照片内容不同而返回不同的风格 id、名称和参数。

## 2. 实现

| 位置 | 作用 |
| --- | --- |
| `app/adapters/deepseek_analysis.py` | DeepSeek 图片分析 Provider；准备去元数据图片、发送 JSON Schema、校验结构化输出 |
| `app/adapters/factory.py` | `deepseek` 模式构造真实照片 Provider；学习流程保留 Mock |
| `app/config.py` | DeepSeek Key/模型/地址/超时/重试配置；运行时默认 `deepseek` |
| `app/services/workflows.py` | 支持请求级 `provider=deepseek`；组装/应用系列一致性 |
| `app/services/series.py` | dHash 签名、相似照片匹配、locked/aligned/context 与黑白系列策略 |
| `app/schemas.py` | `PhotoAnalysisProviderSelection` 增加 `deepseek`；`analysis_version` / `series` |
| `tests/test_deepseek_analysis.py` | 请求合同、结构化输出、重试、超时和错误测试 |

请求链路：

```text
上传图片
  -> 原图校验 / CR3 内嵌预览提取
  -> 去 EXIF、最长边 2048、sRGB JPEG
  -> DeepSeek chat/completions（图片 + 严格 JSON Schema）
  -> StructuredPhotoAnalysis 校验
  -> ImageAnalysis（3 个动态风格 + 参数）
  -> 现有预览 / XMP / Lightroom 链路
```

图片内的文字、文件名和元数据在系统提示词中被明确声明为不可信内容，不能被模型当作指令。
如果模型第一次返回的 JSON 不合格，Provider 会自动用更严格的提醒重试一次。

### 2.1 黑白条件与系列一致性

`PHOTO_PROMPT_VERSION` 从 `photo-analysis-v5` 升到 `photo-analysis-v6`。风格数仍固定为
3 套，但删除了“至少一套必须以非白平衡维度为主”的强制项：黑白只在明暗/光影层次、
线条/几何/建筑、纹理/质感、人物形体/剪影、色彩干扰主体、极简/舞台硬光等场景推荐；
花卉、美食、日落/朝霞、霓虹、彩妆、鲜艳服饰等以色彩为主要信息的照片不给黑白。若给
黑白，必须写 `parameters.overrides` 的 `{"key": "black_and_white", "value": 1}`，
不能只用 `saturation=-100` 模拟，并要在 `best_fit_reason` 写具体受益点。高饱和照片上
出现理由空泛的黑白时，Provider 会做一次质量修复重试。

系列一致性在 Provider 调用前后完成：

```text
照片 -> dHash/宽高比/亮度/饱和度签名（data/signatures/<photo_id>.json）
     -> 与最近已分析且 analysis_version=photo_analysis_v2 的照片比较
     -> locked（其他 session 近似重复）/ aligned（同 session 重分析、连拍、强相似）
        / context（弱相似）
     -> Provider（参考摘要注入用户提示，只作为数据）
     -> 应用 locked 复用或 aligned 黑白过滤/补齐
     -> ImageAnalysis.series 写入 session，UI 展示说明
```

`locked` 精确复用参考的 3 个 style id/name/参数/顺序；`aligned` 要求沿用相近方向，
参数按当前照片测量重算；参考系列没有黑白时，`aligned` 会过滤本次黑白并从参考的彩色
风格补齐到 3 套。旧分析（`analysis_version` 为 `null`）不参与锁定/策略；签名缓存或
系列匹配失败只记日志，不影响本次分析。

## 3. 配置

默认无需设置环境变量：应用启动时会依次读取

1. `AI_PHOTO_AGENT_DEEPSEEK_API_KEY`
2. `DEEPSEEK_API_KEY`
3. `AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE`（仅显式设置时读取）

第 3 项不会隐式读取其他应用的凭据库；Key 不会记录或通过 HTTP 返回。
可用变量：

```powershell
$env:AI_PHOTO_AGENT_ANALYSIS_PROVIDER = "deepseek"
$env:AI_PHOTO_AGENT_DEEPSEEK_MODEL = "deepseek-flash"
$env:AI_PHOTO_AGENT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:AI_PHOTO_AGENT_DEEPSEEK_TIMEOUT_SECONDS = "180"
$env:AI_PHOTO_AGENT_DEEPSEEK_MAX_RETRIES = "1"
```

缺少 Key 时应用拒绝启动；失败返回 502、超时返回 504、结构化输出不合格返回 500，
不会静默回退 Mock。离线测试可显式设置 `AI_PHOTO_AGENT_ANALYSIS_PROVIDER=mock`。

## 4. 真实验证

### 4.1 真实 CR3 上传 + 分析

使用本机真实照片：

```text
D:\Photos\2026\2026-05-23\example.CR3
```

通过运行中的服务执行：

```text
POST /api/photos            -> 200（CR3 6000x4000，render_source=embedded_preview）
POST /api/photos/{id}/analyze -> 200（约 28.3 秒）
```

返回的风格为本次照片专属：

```text
urban-blue-clean   城市街实·清透蓝
leafy-film-warm    暖调胶片·绿意
high-key-minimal   高调极简·留白
```

返回内容与完整参数已保存到 `data/runtime/deepseek-live-analysis.json`（运行产物，
不在 git 中）。同一组动态风格随后通过 `/preview`、`/export`、`/develop` 全部返回 200，
证明动态风格可以继续走预览、XMP 和 Lightroom 参数链路。

### 4.2 动态性对照

同一模型对另一张照片/合成图返回过：

```text
clean-vector-pop    清爽矢量·高色
pastel-storybook    柔雾绘本
dusk-graphic        暮色图形
```

在引入系列一致性之前，同一张真实 CR3 再次分析曾返回 `blue-bike-graphic`、`soft-urban-documentary`、`film-green-canopy`，与第一次的 3 个风格不同。现在同一张照片重传或同一批次照片会命中 `locked`/`aligned` 策略，沿用相近的 style id 和方向，不再把“同图每次都不一样”当作优点；只有画面不相似时才独立生成。

### 4.3 自动化测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -W error
```

结果：**154 passed**。新增用例覆盖：

- 请求 URL、模型、`response_format=json_object`、图片 data URL；
- 结构化输出到领域模型的转换；
- 429/5xx 重试；
- 非法结构化输出自动重试一次；
- 401/超时/非法 JSON 的错误映射；
- 显式凭据文件读取 Key，且不隐式读取 DSH 凭据；
- `deepseek` 工厂与缺失 Key 拒绝启动。

## 5. 已知限制

- `deepseek` 模式下 Phase 2 摄影学习仍使用 Mock；本轮只替换照片诊断链路。
- 分析依赖网络、DeepSeek 账户配额和模型输出质量；耗时通常约 20-60 秒。
- 服务端只保存分析结果，尚未持久化 provider/model/prompt/schema/耗时 provenance；
  后续可把这些字段加入 session。
- 预览仍是 Pillow 近似；Lightroom 参数映射和真机应用需按
  [Lightroom 插件说明](LIGHTROOM_PLUGIN.zh-CN.md) 验证。
