# 上传照片展示、自动旋转与 XMP 下载修复

> 日期：2026-10-24（Asia/Shanghai）  
> 目标：上传区显示照片并自动按拍摄方向旋转；预览/导出对比与上传方向一致；
> 修复预览和导出区 XMP 无法下载的问题。

## 1. 上传区现在展示照片

`app/static/index.html` 的上传区改为“选择文件即自动上传”，并新增：

- `<figure id="uploadedPhotoBox">` + `<img id="uploadedImage">`；
- 文件 input 的 `change` 事件直接触发上传分析，已移除“上传并分析”按钮；
- 上传成功后立即显示照片，不需要等 AI 分析完成；
- caption 显示格式、校正后的尺寸和来源（CR3 内嵌预览 / TIFF 显示预览 / 原图）；
- 重新上传时隐藏旧图；分析失败时照片仍保留，便于判断上传是否成功。

图片使用 `/api/photos/{id}/display`，与下方“原图”对比图共享同一个已校正方向的显示源。

## 2. 自动旋转

### 2.1 JPEG/PNG/TIFF

- 上传时读取 Pillow EXIF Orientation（tag 274）；
- 元数据宽高按方向交换（如 80×40 + Orientation 6 → 40×80）；
- 新增 `display.jpg`：`ImageOps.exif_transpose` 后生成浏览器可直接显示的正向 JPEG；
- 上传卡片和预览区“原图”都使用这个 `display.jpg`，浏览器不再依赖自身 EXIF 猜测。

### 2.2 Canon CR3

CR3 内嵌 JPEG 常常 **没有 EXIF Orientation**，所以浏览器会横着显示。
本次新增：

- 在 CR3 的 `CMT1` EXIF 块中解析 TIFF IFD0 的 Orientation（tag 0x0112）；
- 按 Orientation 2–8 把内嵌 JPEG 旋转/翻转到 `embedded.jpg`；
- 元数据宽高同步交换；
- 分析和预览都从旋转后的 `embedded.jpg` 读取，因此上传图、原图、近似预览方向一致。

实现位置：`app/services/image_formats.py`（`_cr3_exif_orientation`、`write_oriented_jpeg`）
和 `app/services/storage.py`（生成 `display.jpg` / 旋转后的 `embedded.jpg`）。

## 3. 预览与导出的方向一致

- 预览区“原图”用 `display_url`，即已校正的 `display.jpg` / `embedded.jpg`；
- 近似预览用 `_render_source(session)`，CR3 是旋转后的 `embedded.jpg`，JPEG/TIFF 由
  `render_preview` 再执行 `exif_transpose`；
- 因此对比图的两侧保持相同方向；导出 XMP 不包含像素，但预览/对比图已与上传一致。

## 4. XMP 下载修复

服务端在 `app/main.py` 中为所有 `/exports/*.xmp` 响应增加：

```http
Content-Disposition: attachment; filename="..."; filename*=UTF-8''...
```

同时前端在“导出 XMP”成功后主动触发一次下载，并保留可点击的下载链接：

```text
XMP 预设已生成并开始下载；如浏览器拦截，请点击下方链接。
```

这样即使浏览器把 XMP 当作 `text/plain` 打开，也会按附件下载。

## 5. 验证

真实 CR3：`D:\Photos\2026\2026-05-23\example.CR3`（CMT1 Orientation=8）

| 项目 | 修复前 | 修复后 |
| --- | --- | --- |
| 上传元数据 | 6000×4000 横向 | **4000×6000 纵向** |
| 显示图尺寸 | 6000×4000 横向 | **4000×6000 纵向** |
| 预览尺寸 | 横向 | **1067×1600 纵向** |
| 分析 | 需要额外提示旋转 | 直接按正向画面分析 |

真实 JPEG：`example.JPG`（EXIF Orientation=8）

- 元数据：**4000×6000**；
- 显示图：**1365×2048**，方向与预览一致。

XMP：

```text
POST /api/photos/{id}/export -> 200
GET  /exports/....xmp        -> 200
Content-Disposition: attachment; filename="..."; filename*=UTF-8''...
```

浏览器端验证（Edge headless，真实 `example.JPG`）：

- 上传区照片显示尺寸 **1365×2048**（纵向），页面 caption 为 `4000 × 6000`；
- 预览区原图与近似预览分别为 **1365×2048 / 1067×1600**，方向一致；
- 点击“导出 XMP”后浏览器实际下载 `*.xmp`（2733 bytes）；
- 截图：`data/runtime/ui-auto-upload.png`（自动上传，无按钮）、`data/runtime/ui-preview-orientation2.png`。

自动化测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -W error
```

结果：**154 passed**，新增用例覆盖：

- JPEG EXIF Orientation 6 的上传展示与元数据；
- CR3 CMT1 Orientation 8 的显示/预览旋转；
- XMP 下载响应的 `Content-Disposition: attachment`。

## 6. 已知限制

- CR3 显示图仍是完整 4000×6000（约 5.5 MB），大图加载会占用一点带宽；后续可再生成
  2048 长边的单独 `display` 缩略图。
- 只读取标准 EXIF/CR3 CMT1 Orientation；没有手动旋转按钮。
- XMP 仍是 Lightroom 预设文件，不是渲染后的图片；下载内容本身不包含方向信息。
