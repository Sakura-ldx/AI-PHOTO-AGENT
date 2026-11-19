# 技术方向

## 推荐的初始形态

从一个由小型服务层支撑的 Web 应用开始：

- 前端：上传、分析结果、风格选择、预览、预设导出和学习视图。
- 后端服务：图像存储、模型调用、参数转换、预览渲染和参考图分析。
- 模型适配器：为视觉分析、文本生成以及可能的图像转换提供隔离接口。
- 导出模块：基于内部编辑参数，确定性地生成 Lightroom 预设。

这能让产品在浏览器中可用，同时保留后续添加桌面助手或面向 Lightroom 工作流的可能性。

## 建议架构

```text
User Interface
  -> Upload and session management
  -> Editing recommendations
  -> Preview comparison
  -> Lightroom preset export
  -> Photography learning workspace

Application Service
  -> Image metadata extraction
  -> Model orchestration
  -> Editing parameter schema
  -> Preview rendering
  -> Reference image analysis

Adapters
  -> Vision model provider
  -> Image processing engine
  -> Web source or dataset provider
  -> Lightroom preset writer

Storage
  -> Original uploads
  -> Generated previews
  -> Preset exports
  -> Analysis JSON
  -> Reference image metadata
```

## 编辑参数 Schema

先使用内部 schema，再映射到导出格式：

- `exposure`
- `contrast`
- `highlights`
- `shadows`
- `whites`
- `blacks`
- `temperature`
- `tint`
- `vibrance`
- `saturation`
- `texture`
- `clarity`
- `dehaze`
- `tone_curve`
- `hsl`
- `color_grading`
- `sharpening`
- `noise_reduction`
- `crop_suggestion`

内部 schema 应该进行版本化，使模型提示词、预览渲染和 Lightroom 导出可以独立演进。

## Lightroom 导出策略

现代 Lightroom 工作流优先使用 `.xmp` 作为预设输出格式。

实现时应避免把模型响应直接硬编码进 `.xmp`。相反：

1. 模型返回风格理由和归一化的编辑意图。
2. 应用验证并限制参数范围。
3. 内部 schema 存储版本化的值。
4. 导出适配器将内部字段映射到兼容 Lightroom 的 XMP 字段。

## 预览策略

预览渲染可以从使用本地图像处理库的近似效果开始。预览应在内部标记为近似结果，并随着时间持续改进。

优先支持的预览控制：

- 曝光和对比度。
- 白平衡。
- 高光和阴影。
- 饱和度和自然饱和度。
- 色彩分级近似。

## 摄影学习来源策略

学习模块只应摄入以下来源的图像：

- 用户提供的上传图像。
- 明确提供 API 访问的来源。
- 具备兼容许可证的公开数据集。
- 经过人工批准的精选图像列表。

对每一张参考图，在可用时存储来源元数据：

- URL。
- 作者。
- 许可证。
- 访问方式。
- 获取时间戳。

## 主要风险

- Lightroom 预览不一致：本地预览可能无法完全匹配 Lightroom 的渲染效果。
- 版权和来源访问：不受限制的抓取会带来法律和产品风险。
- 模型不一致：风格建议需要验证，并需要有边界的参数范围。
- 成本和延迟：图像分析和预览生成在规模化后可能变得昂贵。
- 用户信任：推荐必须解释为什么选择某个参数或风格。
