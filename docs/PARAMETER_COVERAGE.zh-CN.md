# Lightroom 参数覆盖清单

> 记录日期：2026-11-06（Asia/Shanghai）
> 结论：修图链路现在覆盖 **112 个 Lightroom 显影键**，即 Lightroom「基本 / 色调曲线 / HSL /
> 颜色分级 / 细节 / 镜头校正 / 变换 / 效果」八个面板里的全部全局滑块。局部蒙版、
> 修复工具、红眼、裁切几何这四类不是「显影参数」，由独立子系统维护，见第 4 节。

---

## 1. 唯一事实源

`app/services/parameter_specs.py` 用 `ParameterSpec` 描述每一个可调项：

| 字段 | 含义 |
| --- | --- |
| `key` | 内部参数名（如 `grain_amount`），API / 模型 / 前端都用它 |
| `lr_key` | Lightroom 显影键（如 `GrainAmount`），XMP 里写作 `crs:GrainAmount` |
| `group` | 面板分组：`basic` / `calibration` / `tone_curve` / `detail` / `optics` / `transform` / `effects` |
| `kind` | `int` / `bool` / `enum` / `curve` |
| `minimum` / `maximum` / `neutral` | 取值范围与中性值 |
| `preview` | 本地 Pillow 预览的支持程度：`exact` / `approx` / `none` |
| `label` | 中文显示名，Web 与 Lightroom 共用 |

新增一个参数只需要两处改动：`PARAMETER_SPECS` 加一行，`AdvancedParameters` 加同名字段。
`tests/test_parameter_specs.py::test_advanced_model_matches_the_spec_table` 会拦住两者不一致。

## 2. 覆盖矩阵

### 2.1 原有基础参数（41 项，v1 起）

| 面板 | 参数 | Lightroom 键 |
| --- | --- | --- |
| 基本 | 曝光 / 对比度 / 高光 / 阴影 / 白色 / 黑色 | `Exposure2012` `Contrast2012` `Highlights2012` `Shadows2012` `Whites2012` `Blacks2012` |
| 基本 | 色温 / 色调 | `IncrementalTemperature` `IncrementalTint` |
| 基本 | 纹理 / 清晰度 / 去朦胧 / 自然饱和度 / 饱和度 | `Texture` `Clarity2012` `Dehaze` `Vibrance` `Saturation` |
| 色调曲线 | 点曲线 | `ToneCurveName2012` + `ToneCurvePV2012` |
| HSL / 颜色 | 8 个色相 × 色相/饱和度/明度 | `HueAdjustment*` / `SaturationAdjustment*` / `LuminanceAdjustment*`（24 键） |
| 颜色分级 | 阴影/中间调/高光色轮 + 混合 + 平衡 | `SplitToning*` + `ColorGrade*` |
| 细节 | 锐化数量/半径/细节/蒙版 | `Sharpness` `SharpenRadius` `SharpenDetail` `SharpenEdgeMasking` |
| 细节 | 降噪 明度/颜色 | `LuminanceSmoothing` `ColorNoiseReduction` |

### 2.2 新增参数（56 项，本次）

| 面板 | 参数 | Lightroom 键 | 预览 |
| --- | --- | --- | --- |
| 基本 | 转换为黑白 | `ConvertToGrayscale` | approx |
| 基本 | 黑白混色 8 色 | `GrayMixerRed` … `GrayMixerMagenta` | approx |
| 校准 | 阴影色调 | `ShadowTint` | none |
| 校准 | 红/绿/蓝 原色色相与饱和度 | `RedHue` `RedSaturation` `GreenHue` `GreenSaturation` `BlueHue` `BlueSaturation` | none |
| 色调曲线 | 参数曲线 4 区 + 3 个分割点 | `ParametricShadows` `ParametricDarks` `ParametricLights` `ParametricHighlights` `ParametricShadowSplit` `ParametricMidtoneSplit` `ParametricHighlightSplit` | exact / approx |
| 色调曲线 | 分通道曲线 | `ToneCurvePV2012Red` `ToneCurvePV2012Green` `ToneCurvePV2012Blue` | exact |
| 细节 | 亮度降噪细节/对比度、颜色降噪细节/平滑度 | `LuminanceNoiseReductionDetail` `LuminanceNoiseReductionContrast` `ColorNoiseReductionDetail` `ColorNoiseReductionSmoothness` | approx |
| 镜头校正 | 移除色差 | `AutoLateralCA` | none |
| 镜头校正 | 扭曲度 | `LensManualDistortionAmount` | none |
| 镜头校正 | 镜头暗角 数量/中点 | `VignetteAmount` `VignetteMidpoint` | approx |
| 镜头校正 | 去边 紫/绿 数量与色相范围 | `DefringePurpleAmount` `DefringePurpleHueLo/Hi` `DefringeGreenAmount` `DefringeGreenHueLo/Hi` | approx / none |
| 变换 | Upright 模式 | `PerspectiveUpright` | none |
| 变换 | 垂直/水平/旋转/比例/缩放/X/Y | `PerspectiveVertical` `PerspectiveHorizontal` `PerspectiveRotate` `PerspectiveAspect` `PerspectiveScale` `PerspectiveX` `PerspectiveY` | none |
| 效果 | 裁剪后暗角 6 项 | `PostCropVignetteAmount` `PostCropVignetteMidpoint` `PostCropVignetteRoundness` `PostCropVignetteFeather` `PostCropVignetteHighlightContrast` `PostCropVignetteStyle` | approx / none |
| 效果 | 颗粒 数量/大小/粗糙度 | `GrainAmount` `GrainSize` `GrainFrequency` | approx |

## 3. 参数怎么流到 Lightroom

1. 模型在 `parameters.overrides` 里返回 `{key, value, points}` 数组（每套风格最多 8 项、最多 12 项兜底），
   `build_advanced_from_overrides()` 负责 clamp、丢弃未知键、给分通道曲线做校验。
2. `EditingParametersV1.advanced` 承载结果；`scaled(intensity)` 按中性值线性插值
   （曲线向 `y = x` 插值，`intensity == 0` 时归 `None`），所以强度滑块同样作用于新参数。
3. `build_lightroom_develop_settings()` 与 `build_xmp_preset()` 都从同一份参数生成，
   前者产出 `lightroom_develop_v1` 表给 `LrPhoto:applyDevelopSettings`，后者产出 `lightroom_xmp_v2` 预设。
4. Web 与 Lightroom 插件显示的都是 `describe_parameters()`（`app/services/parameters_text.py`）产出的
   一句话摘要，不会再各自维护一份文案。

## 4. 明确不覆盖的范围

| 能力 | 原因 |
| --- | --- |
| 局部蒙版（径向/线性/主体/天空掩码） | 不是标量显影参数，XMP 里是 `MaskGroupBasedCorrections` 数组，需要几何数据；Pillow 预览也无法近似 |
| 修复 / 去污点 | `RetouchInfo` 依赖 Lightroom 内部的斑点坐标与 AI 填充结果，插件侧无法生成 |
| 红眼 | 同上，属于像素级修复而非参数 |
| 裁切与旋转几何（`Crop*`） | 会改变画面构图，必须由用户交互决定；现在只输出 `crop_suggestion` 文本建议 |
| 白平衡预设枚举 | 依赖相机型号的 profile 列表，跨机器不可移植；只用 色温/色调 相对偏移 |
| 相机配置文件 / 镜头配置文件 | 依赖 Lightroom 本地的 `.dcp` / 镜头数据库，插件不能凭空生成 |

`transform`（变换）组的 8 个键虽然已经能写进参数表，但**模型不会自动给值**——
`MODEL_OVERRIDE_KEYS` 刻意排除了它们，因为透视校正必须配合裁切几何，模型凭空给
`PerspectiveRotate` 会在画面边缘留出空白。需要时由 API 调用方显式传入。

## 5. 验证

- `tests/test_parameter_specs.py`：规格表与模型字段一致、越界 clamp、未知键丢弃、
  XMP 与显影表都带上新键、`scaled(0)` 回到中性、`to_domain()` 正确折叠 overrides。
- `tests/test_preview_advanced.py`：黑白云台忽略 HSL/颜色分级、暗角中心不动、颗粒可复现、
  参数曲线只影响对应区间、分通道曲线只影响单通道。
- `scripts/evaluate_editing.py` 与 `pytest`（当前 290 passed）继续覆盖端到端链路。
