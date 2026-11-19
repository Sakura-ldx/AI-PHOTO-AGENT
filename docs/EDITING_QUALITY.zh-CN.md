# 修图质量提升：量化测量、回渲染校验与预览渲染器 v4

> 状态日期：2026-11-01（Asia/Shanghai）
> 范围：`app/services/measurements.py`（新）、`app/services/edit_resolver.py`（新）、
> `app/services/preview.py`（重写为 `pillow_preview_v4`）、`app/adapters/vision_models.py`
> （提示词 v3）、`app/static/index.html` 与 `dsh-plugin/`（默认强度 100% + 显示实际参数）、
> `scripts/evaluate_editing.py`（验收脚本）。

## 1. 问题与根因

用户在真实夜景人像上报的两个症状：**预览发灰、像蒙了层滤镜**；**三套风格看不出区别**。
排查后确认不是模型"不聪明"，而是链路里三个可度量的缺陷：

| # | 根因 | 证据 |
|---|---|---|
| R1 | 预览渲染器把阴影/高光朝**纯黑/纯白** `composite`（最高 55%/45%），把纯黑抬成灰 | 同一张照片：原图 p1=7、clip_black 0.52%；v3 预览 p1=**40**、clip_black **0.00%** |
| R2 | `exposure` 不是 EV：`brightness = 1 + exposure*0.18`，+1 EV 只提亮 18%，基本看不出来 | `preview.py` v3 `_apply_basic_adjustments` |
| R3 | 模型参数系统性偏保守，且默认 `intensity=0.7` 再削弱 30% | 存档 v2 输出：exposure 0.25 / -0.05 / 0.1，HSL ≤12，temperature ≤8 |
| R4 | 模型没有任何量化依据（看不到直方图/白平衡/剪切率），也没有任何回渲染校验闭环 | `DeepSeekAnalysisProvider.analyze` 只拿到宽高/格式/色彩模式 |

## 2. 改动

### 2.1 量化测量 `app/services/measurements.py`（新增，纯 Pillow，无新依赖）

`measure_file(path)` / `measure_image(image)` → `ImageMeasurements`：每通道的
`mean/std/p01..p99/clip_low/clip_high`、`range_90`、`chroma_mean`、`saturation_mean`、
中性色（S≤42 且 24≤V≤248）的 `neutral_red_ratio` / `neutral_blue_ratio` / `neutral_coverage`、
`deep_shadow_share`、`bright_highlight_share`、`flat_share`，以及派生量
`mid_tone_ev_delta`（相对 18% 灰的 EV）、`warm_bias`、`is_low_key`/`is_high_key`。
`describe_for_prompt()` 把同一批数字渲染成一段确定性的中文文本，供提示词使用。

关键点是"同一把尺子"：**生成参数、校验渲染、验收闸门读的是同一套统计量**，
所以"提灰"这种缺陷可以被程序判定，而不是靠肉眼。

### 2.2 基线校正 + 回渲染校验 `app/services/edit_resolver.py`（新增）

`resolve_parameters(measurements, style_parameters, intensity, observer=...)` 分三步：

1. **基线校正**：按 `tone_targets()`（低键照片把 p50 抬到 `min(96, max(p50*1.6, p50+18))`，
   目标 p95=232）算出曝光基准（最大 ±1.1 EV，阻尼 0.55），再由剪切率/暗部占比/影调跨度
   推出 shadows、highlights、contrast、dehaze、vibrance、temperature；中性覆盖率 <5% 时
   不做白平衡判断。
2. **软膝放大**：`soft_knee(i) = tanh(1.6i)/tanh(1.6)` —— 默认强度 100% 保持线性
   （保证参数 token 稳定性），基线修正与校验步长才走软膝。
3. **回渲染校验**：`observer` 用 `render_image()` 在 512px 上真实渲染并重新测量，
   最多 2 轮，`|p50 与目标差| ≤ 8`、`p95 ≤ 250`、高光剪切 ≤5% 三条件全满足才 `verified=True`；
   observer 抛异常则记 warning 并交回未校验的结果（不阻断出图）。

`intensity == 0` 时直接返回 `EditingParametersV1().scaled(0.0)` 的精确中性值，不测量、不校验。

### 2.3 预览渲染器 v4 `app/services/preview.py`（重写）

- 新增顶层纯函数 `render_image(image, parameters) -> Image`（无 IO、无缩放），
  `render_preview()` 的签名与临时文件语义保持不变。
- 色调统一成**单一 256 项单调 LUT**（线性光 EV 曝光 → encoded S 曲线对比 → smoothstep
  阴影/高光 → tone curve），`lut[0] == 0`，再用 `dark=min(r,1)` / `lift=max(r-1,0)`
  的比例乘实现，**任何分支都不会把纯黑抬起来**。
- 白平衡改在线性光下做 log 增益，并用 Rec.601 亮度守恒归一（±50 时 R/B=1.048、亮度变化 −0.03%）。
- HSL 改为有界混合（不再是 `composite` 到纯饱和/纯黑）。
- 色彩分级改为纯加法分离调色；降噪补上 `noise_reduction.color`（Cb/Cr）；锐化
  `percent = clamp(amount, 0, 150)`、`threshold=4`；clarity/texture 改为大/小半径局部对比。

### 2.4 提示词 v3 `app/adapters/vision_models.py`

`PHOTO_PROMPT_VERSION = "photo-analysis-v3"`。新增 `photo_measurements_block()`，在
DeepSeek / OpenAI / Codex 三个适配器里把测量块插到 prompt 最前（测量失败返回空串，
不影响上传）。系统提示词新增三条硬约束：**量化测量优先于肉眼估计**、
**三个风格至少要在三个维度上拉开差距**、以及带量级的参数标定表
（exposure 按 EV 计、夜景人像 +0.3~+0.9；shadows +25~+60；temperature −8~−25 等），
并明确"不需要的滑块写 0，不要写象征性小数"。

### 2.5 默认强度与可见性

- `PreviewRequest.intensity` / `ExportRequest.intensity` 默认值 `0.7 → 1.0`（静态页、DSH 插件、
  tools 描述、store 默认值同步）。
- 预览成功后网页显示 **`实际生效：曝光 +1.50 EV · 对比 +12 · …`**，直接暴露真实参数
  （此前 `index.html:697` 把 `result.parameters` 整个丢掉了）。

## 3. 实测结果

### 3.1 预览提灰（同一张照片，`uploads/<session-id>/display.jpg`）

| 版本 | p1 | p50 | p99 | clip_black |
|---|---|---|---|---|
| 原图 | 7 | 49 | 189 | 0.009% |
| v3 预览 | **40（+33）** | 65 | 183 | 0.000% |
| v4 预览 | **6（−1）** | 44 | 190 | 0.023% |

### 3.2 端到端（测量 → 解析 → 渲染 → 复测，`scripts/evaluate_editing.py`）

resolver 自动给出 `曝光基准 +0.82 EV`，目标 `p05=14 p50=78 p95=232`，2 轮后 `verified=True`：

```
resolved: exposure=1.5 contrast=12 highlights=-30 shadows=30 temperature=-6 vibrance=15 ...
rendered: p01=8 p05=20 p50=71 p95=218 p99=241 clip_black=0.051% clip_white=0.037%
[PASS] 黑位未被抬灰: p01 +1 (<= 8)
[PASS] 高光未溢出: p95 218 (<= 250)
[PASS] 高光剪切受控: 0.04% (<= 5%)
```

### 3.3 风格离散度（真实 DeepSeek 调用，同一张夜景人像）

| | exposure | contrast | shadows | temperature |
|---|---|---|---|---|
| v2（存档） | 0.25 / −0.05 / 0.10 | +8 / +4 / +10 | 24 / 30 / 18 | 6 / 4 / 2 |
| v3（live） | **0.8 / 1.2 / 0.35** | **+18 / −10 / +30** | **45 / 65 / 22** | **+4 / −8 / −12** |

三个风格分别是暖调电影感、柔亮空气感、冷峻高反差；参数量级从"看不出改动"进入可见区间。

## 4. 如何复现 / 回归

```powershell
# 全量测试
.venv\Scripts\python.exe -m pytest -q

# 对任意照片跑一次完整验收（三条闸门，任一失败 exit 1）
.venv\Scripts\python.exe scripts\evaluate_editing.py `
  --photo uploads\...\display.jpg --exposure 0.8 --contrast 18 --shadows 45 --verbose
```

调参入口：`app/services/edit_resolver.py` 的 `tone_targets()`（目标影调）、
`BASELINE_EXPOSURE_DAMPING`（曝光基准力度）、`EXPOSURE_LIMIT`（风格+基线合成的曝光上限）、
`MAX_VERIFICATION_ROUNDS`（校验轮数）。

## 5. 已知边界

- 预览仍是**近似**：`preview_is_approximate=True` 不变，不等于 Lightroom 渲染；
  差异主要在局部蒙版、镜头/相机配置文件与去噪算法上。
- 仍是全局调整：没有天空/主体蒙版、没有裁切与透视校正（`crop_suggestion` 依然只作文本建议）。
- `verified=False` 只表示在 2 轮内没收敛（例如风格本身的曝光已顶到 `EXPOSURE_LIMIT`），
  出图照常；这类情况会写进 `ResolutionReport.warnings`。
- XMP 仍未用 Lightroom Classic 真机黄金样本认证；参数正确性目前由"像素闸门 + 单测"保证。
