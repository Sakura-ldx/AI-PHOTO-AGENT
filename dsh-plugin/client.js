/**
 * Client half of the AI Photo Agent plugin.
 *
 * Rendering only: a sidebar icon that selects the `photo-agent` main panel and
 * a React view for photo diagnosis, styles, previews, XMP/Lightroom settings,
 * the generated learning-summary knowledge base, and backend status. All photo
 * behavior happens in the existing Python backend behind `/photo-agent/api`.
 */

window.__ModuleLoader__.load({
  id: '@local/dsh-photo-agent',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const { useState, useEffect, useMemo, useRef } = React;

    const NS = 'photo-agent';
    const API = '/photo-agent/api';
    const UI_BUILD = '2026-11-14.2';

    const styles = {
      insert(css) {
        if (typeof document === 'undefined') return () => {};
        const tag = document.createElement('style');
        tag.setAttribute('data-plugin', '@local/dsh-photo-agent');
        tag.textContent = css;
        document.head.appendChild(tag);
        return () => {
          tag.remove();
        };
      },
    };

    const DICT = {
      zh: {
        'panel.title': 'AI 修图',
        'panel.subtitle': '诊断 · 3 套风格 · 近似预览 · XMP / Lightroom',
        'tab.photo': '修图',
        'tab.knowledge': '知识库',
        'tab.status': '状态',
        'action.retry': '重试',
        'action.refresh': '刷新状态',
        'action.reset': '清空当前结果',
        'action.upload': '选择照片',
        'action.analyze': '重新分析',
        'action.preview': '生成预览',
        'action.export': '导出 XMP',
        'action.develop': '生成 Lightroom 参数',
        'action.send': '发送到 Lightroom',
        'action.install': '安装 / 检查插件',
        'action.openFull': '打开完整界面',
        'action.zoom': '放大查看',
        'zoom.fit': '适应窗口',
        'zoom.reset': '适应窗口',
        'zoom.close': '关闭',
        'zoom.open': '新窗口打开',
        'zoom.hint': '点击图片可放大查看，方便检查细节。',
        'zoom.help': '滚轮缩放（百分比相对适应窗口）；放大后按住图片拖动；双击画面或点“适应窗口”复位；Esc 关闭。',
        'zoom.title': '图像查看',
        'status.backend': '后端',
        'status.lightroom': 'Lightroom',
        'status.ready': '已就绪',
        'status.starting': '正在启动',
        'status.notRunning': '未启动',
        'status.managed': '插件托管',
        'status.external': '外部实例',
        'status.port': '端口',
        'status.provider': 'Provider',
        'status.python': 'Python',
        'status.dataDir': '数据目录',
        'status.detected': '已检测到',
        'status.notDetected': '未检测到',
        'status.pluginInstalled': '插件已注册',
        'status.pluginMissing': '插件未注册',
        'status.log': '后端日志尾部',
        'status.noLog': '暂无日志。',
        'upload.title': '上传照片',
        'upload.hint': '选择文件后自动上传并分析。支持 JPG/JPEG、PNG、TIF/TIFF 和 Canon CR3；CR3 使用相机内嵌 JPEG 预览。',
        'upload.analyzing': '正在上传并分析，通常需要 20-60 秒…',
        'upload.tooLarge': '图片 {size} MB，超过 {limit} MB 上传上限；请先压缩或导出为 JPEG 再试。',
        'upload.replace': '更换照片',
        'analysis.title': '图片诊断',
        'analysis.byAi': '诊断和 3 套风格由后端配置的 AI 视觉模型实时生成。',
        'analysis.technical': '技术',
        'analysis.composition': '构图',
        'analysis.color': '色彩 / 影调',
        'styles.title': '选择风格',
        'styles.aiPick': 'AI 首选',
        'styles.reason': '最适合理由',
        'styles.impact': '预期视觉影响',
        'styles.rationale': '为什么这样调',
        'preview.title': '预览与导出',
        'preview.original': '原图',
        'preview.approximate': '近似预览',
        'preview.note': '预览是 Pillow 近似结果，不等同于 Lightroom 渲染。',
        'preview.empty': '选择风格后点击“生成预览”。',
        'export.title': 'Lightroom 预设',
        'export.preset': '预设名',
        'export.mapping': '映射版本',
        'export.download': '下载 XMP',
        'export.none': '还没有生成 XMP。',
        'develop.title': 'Lightroom 显影参数',
        'develop.key': '关键参数',
        'develop.none': '还没有生成显影参数。',
        'lightroom.title': 'Lightroom 联动',
        'lightroom.message': '发送后请在 Lightroom 中选中照片，运行“图库 > 增效工具附加功能 > AI Photo Agent - 应用 Web 端待处理修图”。',
        'knowledge.title': '知识库',
        'knowledge.refresh': '刷新知识库',
        'knowledge.rebuild': '为当前照片重新生成总结',
        'knowledge.needPhoto': '先在「修图」中上传并分析一张照片。',
        'knowledge.empty': '还没有学习总结。在「修图」里上传并分析照片后会自动生成。',
        'knowledge.statsCount': '学习总结',
        'knowledge.statsCategories': '主分类',
        'knowledge.statsIntensity': '平均推荐强度',
        'knowledge.categories': '主分类',
        'knowledge.tags': '标签筛选',
        'knowledge.clearFilters': '清除筛选',
        'knowledge.directions': '查看修图方向',
        'knowledge.loadingDirections': '正在读取修图方向…',
        'knowledge.intensity': '推荐强度',
        'knowledge.parameters': '参数总结',
        'knowledge.preview': '推荐强度预览',
        'knowledge.rebuilt': '学习总结已重新生成。',
        'knowledge.filtered': '已筛选',
        'knowledge.missing': '这条学习总结已不存在，请刷新。',
        'busy.ensure': '正在启动后端…',
        'busy.upload': '正在上传…',
        'busy.analyze': '正在分析…',
        'busy.preview': '正在生成预览…',
        'busy.export': '正在导出 XMP…',
        'busy.develop': '正在生成参数…',
        'busy.send': '正在写入 Lightroom…',
        'busy.install': '正在检查插件…',
        'busy.knowledge': '正在读取知识库…',
        'busy.lightroom': '正在读取 Lightroom 状态…',
        'notice.analyzed': '分析完成，请选择一个风格。',
        'notice.previewed': '预览已生成。',
        'notice.exported': 'XMP 已生成。',
        'notice.developed': 'Lightroom 参数已生成。',
        'notice.sent': '已写入预设并排队。请在 Lightroom 中执行应用菜单。',
        'notice.installed': 'Lightroom 插件检查/安装完成。',
        'notice.reset': '已清空当前结果。',
        'error.title': '操作失败',
        'error.ensure': '后端启动失败',
        'error.unknown': '未知错误',
        'photo.size': '尺寸',
        'photo.format': '格式',
        'photo.renderSource': '渲染源',
        'preview.intensity': '强度',
        'develop.exposure': '曝光',
        'develop.contrast': '对比',
        'develop.highlights': '高光',
        'develop.shadows': '阴影',
        'develop.temperature': '色温',
        'develop.vibrance': '鲜艳度',
        'develop.clarity': '清晰度',
        'error.panelRender': '面板渲染失败',
        'error.networkHint': '本地服务连接中断或请求被中止，请刷新页面后重试，并确认图片没有超过上传上限（connection lost or request aborted; refresh and retry）。',
        'footer.privacy': '照片只发送给后端配置的 AI Provider；插件不额外上传。',
      },
      en: {
        'panel.title': 'AI Photo Agent',
        'panel.subtitle': 'Diagnosis · 3 styles · preview · XMP / Lightroom',
        'tab.photo': 'Edit',
        'tab.knowledge': 'Knowledge',
        'tab.status': 'Status',
        'action.retry': 'Retry',
        'action.refresh': 'Refresh status',
        'action.reset': 'Clear current result',
        'action.upload': 'Choose photo',
        'action.analyze': 'Re-analyze',
        'action.preview': 'Generate preview',
        'action.export': 'Export XMP',
        'action.develop': 'Generate Lightroom settings',
        'action.send': 'Send to Lightroom',
        'action.install': 'Install / check plugin',
        'action.openFull': 'Open full UI',
        'action.zoom': 'Enlarge',
        'zoom.fit': 'Fit',
        'zoom.reset': 'Fit',
        'zoom.close': 'Close',
        'zoom.open': 'Open in new tab',
        'zoom.hint': 'Click an image to enlarge it and inspect details.',
        'zoom.help': 'Scroll to zoom (percent is relative to Fit); once enlarged, drag the image to pan; double-click or click Fit to reset; press Esc to close.',
        'zoom.title': 'Image viewer',
        'status.backend': 'Backend',
        'status.lightroom': 'Lightroom',
        'status.ready': 'Ready',
        'status.starting': 'Starting',
        'status.notRunning': 'Not running',
        'status.managed': 'Managed',
        'status.external': 'External',
        'status.port': 'Port',
        'status.provider': 'Provider',
        'status.python': 'Python',
        'status.dataDir': 'Data directory',
        'status.detected': 'Detected',
        'status.notDetected': 'Not detected',
        'status.pluginInstalled': 'Plugin registered',
        'status.pluginMissing': 'Plugin not registered',
        'status.log': 'Backend log tail',
        'status.noLog': 'No log yet.',
        'upload.title': 'Upload a photo',
        'upload.hint': 'Choose a file to upload and analyze automatically. JPG/JPEG, PNG, TIF/TIFF and Canon CR3 are supported; CR3 uses the embedded JPEG preview.',
        'upload.analyzing': 'Uploading and analyzing, usually 20-60 seconds…',
        'upload.tooLarge': 'This file is {size} MB, over the {limit} MB upload limit. Compress it or export a JPEG first.',
        'upload.replace': 'Replace photo',
        'analysis.title': 'Image diagnosis',
        'analysis.byAi': 'Diagnosis and the three styles are generated by the backend AI vision model.',
        'analysis.technical': 'Technical',
        'analysis.composition': 'Composition',
        'analysis.color': 'Color / tone',
        'styles.title': 'Choose a style',
        'styles.aiPick': 'AI pick',
        'styles.reason': 'Best fit',
        'styles.impact': 'Expected impact',
        'styles.rationale': 'Why',
        'preview.title': 'Preview and export',
        'preview.original': 'Original',
        'preview.approximate': 'Approximate preview',
        'preview.note': 'The preview is a Pillow approximation, not a Lightroom render.',
        'preview.empty': 'Choose a style, then generate a preview.',
        'export.title': 'Lightroom preset',
        'export.preset': 'Preset name',
        'export.mapping': 'Mapping version',
        'export.download': 'Download XMP',
        'export.none': 'No XMP generated yet.',
        'develop.title': 'Lightroom develop settings',
        'develop.key': 'Key parameters',
        'develop.none': 'No develop settings generated yet.',
        'lightroom.title': 'Lightroom bridge',
        'lightroom.message': 'After sending, select the photo in Lightroom and run “Library > Plug-in Extras > AI Photo Agent - Apply pending web edit”.',
        'knowledge.title': 'Knowledge',
        'knowledge.refresh': 'Refresh knowledge',
        'knowledge.rebuild': 'Rebuild summary for current photo',
        'knowledge.needPhoto': 'Upload and analyze a photo in Edit first.',
        'knowledge.empty': 'No learning summaries yet. Upload and analyze a photo in Edit; a summary is generated automatically.',
        'knowledge.statsCount': 'Summaries',
        'knowledge.statsCategories': 'Categories',
        'knowledge.statsIntensity': 'Average recommended strength',
        'knowledge.categories': 'Categories',
        'knowledge.tags': 'Filter by tag',
        'knowledge.clearFilters': 'Clear filters',
        'knowledge.directions': 'View editing directions',
        'knowledge.loadingDirections': 'Loading editing directions…',
        'knowledge.intensity': 'Recommended strength',
        'knowledge.parameters': 'Parameter summary',
        'knowledge.preview': 'Recommended-strength preview',
        'knowledge.rebuilt': 'The learning summary was rebuilt.',
        'knowledge.filtered': 'Filtered',
        'knowledge.missing': 'This learning summary no longer exists; refresh.',
        'busy.ensure': 'Starting the backend…',
        'busy.upload': 'Uploading…',
        'busy.analyze': 'Analyzing…',
        'busy.preview': 'Generating preview…',
        'busy.export': 'Exporting XMP…',
        'busy.develop': 'Generating settings…',
        'busy.send': 'Writing to Lightroom…',
        'busy.install': 'Checking the plugin…',
        'busy.knowledge': 'Reading knowledge…',
        'busy.lightroom': 'Reading Lightroom status…',
        'notice.analyzed': 'Analysis complete. Choose a style.',
        'notice.previewed': 'Preview generated.',
        'notice.exported': 'XMP generated.',
        'notice.developed': 'Lightroom settings generated.',
        'notice.sent': 'Preset written and queued. Run the Lightroom menu command.',
        'notice.installed': 'Lightroom plugin check/install finished.',
        'notice.reset': 'Current result cleared.',
        'error.title': 'Operation failed',
        'error.ensure': 'Backend failed to start',
        'error.unknown': 'Unknown error',
        'photo.size': 'Size',
        'photo.format': 'Format',
        'photo.renderSource': 'Render source',
        'preview.intensity': 'Strength',
        'develop.exposure': 'Exposure',
        'develop.contrast': 'Contrast',
        'develop.highlights': 'Highlights',
        'develop.shadows': 'Shadows',
        'develop.temperature': 'Temperature',
        'develop.vibrance': 'Vibrance',
        'develop.clarity': 'Clarity',
        'error.panelRender': 'Panel render failed',
        'error.networkHint': 'The local service connection was interrupted or the request was aborted. Refresh the page and retry, and check that the image is not over the upload limit.',
        'footer.privacy': 'Photos are only sent to the AI provider configured by the backend; the plugin uploads nothing extra.',
      },
    };

    const CSS = `
.photo-root{display:flex;flex-direction:column;height:100%;min-height:0;overflow:auto;padding:20px 24px 28px;box-sizing:border-box;color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-base);}
.photo-head{display:flex;flex-direction:column;gap:10px;margin-bottom:16px;}
.photo-title-row{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;}
.photo-title{font-size:20px;font-weight:650;margin:0;line-height:1.3;}
.photo-subtitle{font-size:12px;color:var(--dsw-alias-label-secondary);margin:4px 0 0;line-height:1.6;}
.photo-chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
.photo-chip{font-size:11px;border-radius:999px;padding:4px 10px;border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-secondary);white-space:nowrap;}
.photo-chip-ok{color:var(--dsw-alias-state-success-primary);border-color:var(--dsw-alias-state-success-primary);}
.photo-chip-warn{color:var(--dsw-alias-state-warn-primary);border-color:var(--dsw-alias-state-warn-primary);}
.photo-chip-error{color:var(--dsw-alias-state-error-primary);border-color:var(--dsw-alias-state-error-primary);}
.photo-tabs{display:flex;gap:6px;border-bottom:1px solid var(--dsw-alias-border-l1);margin-bottom:16px;}
.photo-tab{border:0;background:transparent;color:var(--dsw-alias-label-secondary);font-size:13px;padding:9px 12px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;}
.photo-tab.active{color:var(--dsw-alias-label-primary);border-bottom-color:var(--dsw-alias-brand-primary);font-weight:600;}
.photo-card{border:1px solid var(--dsw-alias-border-l1);border-radius:12px;background:var(--dsw-alias-bg-layer-1);padding:16px;margin-bottom:14px;}
.photo-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap;}
.photo-card-title{font-size:14px;font-weight:650;margin:0;}
.photo-muted{font-size:12px;color:var(--dsw-alias-label-secondary);line-height:1.7;}
.photo-note{font-size:12px;color:var(--dsw-alias-label-secondary);line-height:1.7;margin:8px 0 0;}
.photo-alert{font-size:12px;border-radius:8px;padding:9px 12px;border:1px solid var(--dsw-alias-border-l1);color:var(--dsw-alias-label-secondary);margin-bottom:12px;line-height:1.7;white-space:pre-wrap;}
.photo-alert-error{border-color:var(--dsw-alias-state-error-primary);color:var(--dsw-alias-state-error-primary);}
.photo-alert-warn{border-color:var(--dsw-alias-state-warn-primary);color:var(--dsw-alias-state-warn-primary);}
.photo-alert-ok{border-color:var(--dsw-alias-state-success-primary);color:var(--dsw-alias-state-success-primary);}
.photo-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);border-radius:8px;font-size:12px;padding:7px 12px;cursor:pointer;line-height:1.4;}
.photo-btn:hover:not(:disabled){border-color:var(--dsw-alias-brand-primary);}
.photo-btn:disabled{opacity:.5;cursor:not-allowed;}
.photo-btn-primary{background:var(--dsw-alias-brand-primary);border-color:var(--dsw-alias-brand-primary);color:#fff;}
.photo-btn-danger{color:var(--dsw-alias-state-error-primary);border-color:var(--dsw-alias-state-error-primary);}
.photo-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.photo-input,.photo-textarea{width:100%;box-sizing:border-box;border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-base);color:var(--dsw-alias-label-primary);border-radius:8px;padding:8px 10px;font-size:12px;font-family:inherit;}
.photo-input:focus,.photo-textarea:focus{outline:none;border-color:var(--dsw-alias-brand-primary);}
.photo-file{font-size:12px;color:var(--dsw-alias-label-secondary);}
.photo-thumb{max-width:100%;max-height:520px;border-radius:10px;display:block;object-fit:contain;background:var(--dsw-alias-bg-layer-2);}
.photo-photo-line{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start;}
.photo-photo-meta{font-size:12px;color:var(--dsw-alias-label-secondary);line-height:1.8;min-width:180px;}
.photo-grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));}
.photo-style{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;background:var(--dsw-alias-bg-base);padding:12px;cursor:pointer;display:flex;flex-direction:column;gap:8px;min-height:150px;}
.photo-style:hover{border-color:var(--dsw-alias-brand-primary);}
.photo-style.selected{border-color:var(--dsw-alias-brand-primary);box-shadow:0 0 0 1px var(--dsw-alias-brand-primary) inset;}
.photo-style-top{display:flex;align-items:center;justify-content:space-between;gap:8px;}
.photo-style-name{font-size:13px;font-weight:650;}
.photo-badge{font-size:10px;border-radius:999px;padding:2px 8px;background:var(--dsw-alias-brand-primary);color:#fff;white-space:nowrap;}
.photo-style-desc{font-size:12px;color:var(--dsw-alias-label-secondary);line-height:1.65;}
.photo-style-field{font-size:11px;color:var(--dsw-alias-label-secondary);line-height:1.6;}
.photo-style-field strong{color:var(--dsw-alias-label-primary);font-weight:600;}
.photo-slider-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
.photo-slider{flex:1;min-width:180px;accent-color:var(--dsw-alias-brand-primary);}
.photo-compare{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px;}
@media (max-width:720px){.photo-compare{grid-template-columns:1fr;}}
.photo-compare-cell{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;overflow:hidden;background:var(--dsw-alias-bg-layer-2);}
.photo-compare-label{font-size:11px;color:var(--dsw-alias-label-secondary);padding:6px 10px;border-bottom:1px solid var(--dsw-alias-border-l1);}
.photo-compare-cell img{display:block;width:100%;max-height:430px;object-fit:contain;background:var(--dsw-alias-bg-base);}
.photo-zoomable{position:relative;display:block;width:100%;padding:0;border:0;background:transparent;color:var(--dsw-alias-label-primary);cursor:zoom-in;text-align:center;border-radius:10px;overflow:hidden;font:inherit;}
.photo-zoomable:hover img{opacity:.96;}
.photo-zoomable:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:2px;}
.photo-zoomable img{display:block;width:100%;height:auto;background:var(--dsw-alias-bg-layer-2);}
.photo-zoom-badge{position:absolute;right:8px;bottom:8px;font-size:11px;line-height:1.2;padding:4px 8px;border-radius:999px;background:var(--dsw-alias-bg-overlay);border:1px solid var(--dsw-alias-border-l2);color:var(--dsw-alias-label-primary);opacity:0;transition:opacity .15s ease;pointer-events:none;}
.photo-zoomable:hover .photo-zoom-badge,.photo-zoomable:focus-visible .photo-zoom-badge{opacity:1;}
.photo-thumb-wrap{max-width:560px;margin:0;display:block;}
.photo-thumb-wrap img{width:auto;max-width:100%;max-height:520px;border-radius:10px;object-fit:contain;}
.photo-export-thumb{max-height:260px;object-fit:contain;border-radius:8px;}
.photo-lightbox{position:fixed;inset:0;z-index:1500;display:flex;flex-direction:column;background:rgba(8,10,14,.9);background:color-mix(in srgb,var(--dsw-alias-bg-base) 76%,#05070a);}
.photo-lightbox-head{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1);flex-wrap:wrap;}
.photo-lightbox-title{font-size:13px;font-weight:650;margin-right:auto;min-width:120px;color:var(--dsw-alias-label-primary);}
.photo-lightbox-tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap;}
.photo-lightbox-btn{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);border-radius:7px;font-size:12px;padding:5px 10px;cursor:pointer;line-height:1.4;}
.photo-lightbox-btn:hover{border-color:var(--dsw-alias-brand-primary);}
.photo-lightbox-size{font-size:11px;color:var(--dsw-alias-label-secondary);padding:0 4px;white-space:nowrap;}
.photo-lightbox-zoom{font-size:11px;color:var(--dsw-alias-brand-primary);padding:0 4px;white-space:nowrap;font-variant-numeric:tabular-nums;}
.photo-lightbox-stage{flex:1;min-height:0;overflow:hidden;position:relative;padding:16px;display:flex;align-items:center;justify-content:center;text-align:center;background:transparent;}
.photo-lightbox-stage img{display:block;max-width:100%;max-height:calc(100vh - 150px);width:auto;height:auto;user-select:none;-webkit-user-drag:none;box-shadow:0 10px 44px rgba(0,0,0,.4);background:var(--dsw-alias-bg-layer-2);}
.photo-lightbox-foot{padding:8px 16px 12px;border-top:1px solid var(--dsw-alias-border-l1);text-align:center;font-size:11px;line-height:1.7;color:var(--dsw-alias-label-secondary);}
.photo-list{margin:0;padding-left:18px;font-size:12px;color:var(--dsw-alias-label-secondary);line-height:1.75;}
.photo-list li{margin:2px 0;}
.photo-kv{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;font-size:12px;line-height:1.7;}
.photo-kv dt{color:var(--dsw-alias-label-secondary);}
.photo-kv dd{margin:0;color:var(--dsw-alias-label-primary);word-break:break-all;}
.photo-table{width:100%;border-collapse:collapse;font-size:12px;}
.photo-table th,.photo-table td{border-bottom:1px solid var(--dsw-alias-border-l1);padding:6px 8px;text-align:left;}
.photo-table th{color:var(--dsw-alias-label-secondary);font-weight:500;}
.photo-pre{font-size:11px;line-height:1.6;white-space:pre-wrap;word-break:break-all;background:var(--dsw-alias-bg-layer-2);border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:10px;max-height:220px;overflow:auto;margin:8px 0 0;color:var(--dsw-alias-label-secondary);}
.photo-spinner{font-size:12px;color:var(--dsw-alias-label-secondary);padding:10px 0;line-height:1.7;}
.photo-icon-wrap{position:relative;display:inline-flex;align-items:center;justify-content:center;}
.photo-footer{margin-top:auto;padding-top:18px;font-size:11px;color:var(--dsw-alias-label-secondary);line-height:1.7;}
.photo-field{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--dsw-alias-label-secondary);}
.photo-fields{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin:12px 0;}
.photo-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.photo-knowledge-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:2px 0 12px;}
.photo-knowledge-stats{display:flex;gap:14px;flex-wrap:wrap;align-items:baseline;margin-bottom:10px;}
.photo-knowledge-stats strong{font-size:15px;}
.photo-knowledge-group{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0;}
.photo-knowledge-group-label{font-size:11px;color:var(--dsw-alias-label-secondary);min-width:64px;}
.photo-chip-btn{cursor:pointer;font:inherit;}
.photo-chip-btn[aria-pressed="true"]{border-color:var(--dsw-alias-brand-primary);color:var(--dsw-alias-brand-primary);}
.photo-knowledge-grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));}
.photo-knowledge-card{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;background:var(--dsw-alias-bg-layer-1);padding:12px;display:flex;flex-direction:column;gap:9px;min-width:0;}
.photo-knowledge-card-head{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;}
.photo-knowledge-thumb{display:block;width:100%;height:176px;object-fit:cover;border-radius:8px;background:var(--dsw-alias-bg-layer-2);}
.photo-knowledge-tags{display:flex;flex-wrap:wrap;gap:6px;}
.photo-knowledge-summary{font-size:12px;color:var(--dsw-alias-label-secondary);line-height:1.7;margin:0;}
.photo-knowledge-direction{border-top:1px dashed var(--dsw-alias-border-l1);padding-top:9px;margin-top:3px;display:flex;flex-direction:column;gap:5px;}
.photo-knowledge-direction-head{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;}
.photo-knowledge-direction-name{font-size:12px;font-weight:650;}
.photo-knowledge-direction-note{font-size:11px;color:var(--dsw-alias-label-secondary);line-height:1.65;margin:0;white-space:pre-wrap;}
.photo-knowledge-preview{display:block;max-width:100%;max-height:240px;object-fit:contain;border-radius:8px;background:var(--dsw-alias-bg-base);}
.photo-knowledge-empty{font-size:12px;color:var(--dsw-alias-label-secondary);line-height:1.8;padding:12px 2px;}
`;

    let localeService;

    function errorText(error, t) {
      if (error instanceof Error) {
        const message = error.message || String(error);
        if (/failed to fetch|networkerror|load failed|err_connection/i.test(message)) {
          const hint = typeof t === 'function' ? t('error.networkHint') : '';
          return hint ? `${message} —— ${hint}` : message;
        }
        return message;
      }
      return String(error);
    }

    async function api(path, options) {
      const response = await fetch(`${API}${path}`, {
        cache: 'no-store',
        ...options,
      });
      let payload;
      try {
        payload = await response.json();
      } catch {
        payload = { ok: false, error: `HTTP ${response.status}` };
      }
      if (!payload || payload.ok !== true) {
        throw new Error((payload && payload.error) || `HTTP ${response.status}`);
      }
      return payload;
    }

    function postJson(path, body) {
      return api(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
    }

    function assetUrl(value) {
      if (!value) return '';
      if (/^https?:\/\//i.test(value)) return value;
      return `${API}/asset?path=${encodeURIComponent(value)}`;
    }

    async function uploadFile(file) {
      const response = await fetch(`${API}/upload`, {
        method: 'POST',
        cache: 'no-store',
        headers: {
          'Content-Type': file.type || 'application/octet-stream',
          'x-photo-filename': encodeURIComponent(file.name || 'upload.jpg'),
        },
        body: file,
      });
      let payload;
      try {
        payload = await response.json();
      } catch {
        payload = { ok: false, error: `HTTP ${response.status}` };
      }
      if (!payload || payload.ok !== true) {
        throw new Error((payload && payload.error) || `HTTP ${response.status}`);
      }
      return payload.photo;
    }

    function percent(value) {
      const number = Number(value);
      if (!Number.isFinite(number)) return '—';
      return `${Math.round(number * 100)}%`;
    }

    function formatKnowledgeTime(value) {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString();
    }

    function useDictionary() {
      const [locale, setLocale] = useState('zh');
      useEffect(() => {
        const service = localeService;
        if (!service || typeof service.subscribe !== 'function') return undefined;
        const read = () => {
          try {
            const snapshot = service.getSnapshot();
            setLocale(String(snapshot && snapshot.active).startsWith('en') ? 'en' : 'zh');
          } catch {
            setLocale('zh');
          }
        };
        read();
        return service.subscribe(read);
      }, []);
      const t = useMemo(() => {
        const table = DICT[locale] || DICT.zh;
        return (key) => table[key] || DICT.zh[key] || key;
      }, [locale]);
      return { t, locale };
    }

    function useLightbox() {
      const [image, setImage] = useState(null);
      const open = (next) => {
        if (next && next.src) setImage(next);
      };
      const close = () => setImage(null);
      return { image, open, close };
    }

    function ZoomableImage({
      src,
      alt,
      title,
      onZoom,
      t,
      className,
      imageClassName,
      imageStyle,
      buttonStyle,
    }) {
      if (!src) return null;
      const label = title || alt || '';
      return h(
        'button',
        {
          type: 'button',
          className: `photo-zoomable${className ? ` ${className}` : ''}`,
          style: buttonStyle,
          onClick: () => onZoom({ src, title: label, alt: alt || label }),
          title: t('action.zoom'),
          'aria-label': `${t('action.zoom')}：${label}`,
        },
        h('img', {
          src,
          alt: alt || label,
          className: imageClassName,
          style: imageStyle,
          draggable: false,
        }),
        h('span', { className: 'photo-zoom-badge' }, t('action.zoom')),
      );
    }

    const MIN_ZOOM_SCALE = 1;
    const MAX_ZOOM_SCALE = 8;
    const STAGE_PADDING = 32;

    function Lightbox({ image, onClose, t }) {
      const [natural, setNatural] = useState(null);
      const [view, setView] = useState({ scale: 1, x: 0, y: 0 });
      const [dragging, setDragging] = useState(false);
      const viewRef = useRef({ scale: 1, x: 0, y: 0 });
      const stageRef = useRef(null);
      const imgRef = useRef(null);
      const dragRef = useRef(null);
      const wheelHandlerRef = useRef(null);

      function clampView(scale, x, y) {
        const nextScale = Math.min(MAX_ZOOM_SCALE, Math.max(MIN_ZOOM_SCALE, scale));
        const stage = stageRef.current;
        const img = imgRef.current;
        if (!stage || !img || nextScale <= MIN_ZOOM_SCALE + 0.0001) {
          return { scale: MIN_ZOOM_SCALE, x: 0, y: 0 };
        }
        const stageWidth = Math.max(1, stage.clientWidth - STAGE_PADDING);
        const stageHeight = Math.max(1, stage.clientHeight - STAGE_PADDING);
        const imageWidth = img.offsetWidth || img.naturalWidth || 0;
        const imageHeight = img.offsetHeight || img.naturalHeight || 0;
        const maxX = Math.max(0, (imageWidth * nextScale - stageWidth) / 2);
        const maxY = Math.max(0, (imageHeight * nextScale - stageHeight) / 2);
        return {
          scale: nextScale,
          x: Math.min(maxX, Math.max(-maxX, Number.isFinite(x) ? x : 0)),
          y: Math.min(maxY, Math.max(-maxY, Number.isFinite(y) ? y : 0)),
        };
      }

      function applyView(scale, x, y) {
        const next = clampView(scale, x, y);
        viewRef.current = next;
        setView(next);
        return next;
      }

      function resetView() {
        viewRef.current = { scale: MIN_ZOOM_SCALE, x: 0, y: 0 };
        setView({ scale: MIN_ZOOM_SCALE, x: 0, y: 0 });
      }

      function zoomAt(nextScale, clientX, clientY) {
        const stage = stageRef.current;
        const current = viewRef.current;
        if (!stage) return;
        if (nextScale <= MIN_ZOOM_SCALE + 0.0001) {
          resetView();
          return;
        }
        const rect = stage.getBoundingClientRect();
        const anchorX = clientX - rect.left - rect.width / 2;
        const anchorY = clientY - rect.top - rect.height / 2;
        const ratio = nextScale / current.scale;
        applyView(
          nextScale,
          anchorX - (anchorX - current.x) * ratio,
          anchorY - (anchorY - current.y) * ratio,
        );
      }

      wheelHandlerRef.current = (event) => {
        if (!image) return;
        event.preventDefault();
        const current = viewRef.current;
        const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 100 : 1;
        const delta = event.deltaY * unit;
        if (!delta) return;
        const factor = Math.exp(-delta * 0.0015);
        const nextScale = Math.min(
          MAX_ZOOM_SCALE,
          Math.max(MIN_ZOOM_SCALE, current.scale * factor),
        );
        if (Math.abs(nextScale - current.scale) < 0.0001) return;
        zoomAt(nextScale, event.clientX, event.clientY);
      };

      useEffect(() => {
        if (!image || typeof document === 'undefined') return undefined;
        const previous = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        return () => {
          document.body.style.overflow = previous;
        };
      }, [image]);

      useEffect(() => {
        if (!image || typeof document === 'undefined') return undefined;
        const onKeyDown = (event) => {
          if (event.key === 'Escape') onClose();
        };
        document.addEventListener('keydown', onKeyDown);
        return () => document.removeEventListener('keydown', onKeyDown);
      }, [image, onClose]);

      useEffect(() => {
        const stage = stageRef.current;
        if (!image || !stage) return undefined;
        const onWheel = (event) => wheelHandlerRef.current?.(event);
        stage.addEventListener('wheel', onWheel, { passive: false });
        return () => stage.removeEventListener('wheel', onWheel);
      }, [image]);

      useEffect(() => {
        if (!image) return undefined;
        const onResize = () => resetView();
        window.addEventListener('resize', onResize);
        return () => window.removeEventListener('resize', onResize);
      }, [image]);

      if (!image) return null;

      const startDrag = (event) => {
        if (event.button !== 0) return;
        if (viewRef.current.scale <= MIN_ZOOM_SCALE + 0.001) return;
        const stage = stageRef.current;
        if (!stage) return;
        event.preventDefault();
        dragRef.current = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          originX: viewRef.current.x,
          originY: viewRef.current.y,
        };
        setDragging(true);
        try {
          stage.setPointerCapture(event.pointerId);
        } catch {
          /* pointer capture is an enhancement */
        }
      };

      const moveDrag = (event) => {
        const drag = dragRef.current;
        if (!drag) return;
        event.preventDefault();
        applyView(
          viewRef.current.scale,
          drag.originX + (event.clientX - drag.startX),
          drag.originY + (event.clientY - drag.startY),
        );
      };

      const endDrag = (event) => {
        const drag = dragRef.current;
        if (!drag) return;
        dragRef.current = null;
        setDragging(false);
        const stage = stageRef.current;
        if (stage && event && drag.pointerId !== undefined) {
          try {
            stage.releasePointerCapture(drag.pointerId);
          } catch {
            /* ignore */
          }
        }
      };

      const zoomed = view.scale > MIN_ZOOM_SCALE + 0.001;
      const imageStyle = {
        width: 'auto',
        height: 'auto',
        maxWidth: '100%',
        maxHeight: 'calc(100vh - 150px)',
        objectFit: 'contain',
        transformOrigin: 'center center',
        transform: `translate3d(${view.x}px, ${view.y}px, 0) scale(${view.scale})`,
        willChange: 'transform',
        cursor: zoomed ? (dragging ? 'grabbing' : 'grab') : 'zoom-in',
      };

      return h(
        'div',
        {
          className: 'photo-lightbox',
          role: 'dialog',
          'aria-modal': 'true',
          'aria-label': t('zoom.title'),
          onClick: (event) => {
            if (event.target === event.currentTarget) onClose();
          },
        },
        h(
          'div',
          { className: 'photo-lightbox-head' },
          h('div', { className: 'photo-lightbox-title' }, image.title || image.alt || t('zoom.title')),
          h(
            'div',
            { className: 'photo-lightbox-tools' },
            h(
              'span',
              { className: 'photo-lightbox-size' },
              natural ? `${natural.width} × ${natural.height}` : '',
            ),
            h(
              'span',
              { className: 'photo-lightbox-zoom' },
              zoomed ? `${Math.round(view.scale * 100)}%` : t('zoom.fit'),
            ),
            h(
              'button',
              { type: 'button', className: 'photo-lightbox-btn', onClick: resetView },
              t('zoom.reset'),
            ),
            h(
              'a',
              {
                className: 'photo-lightbox-btn',
                href: image.src,
                target: '_blank',
                rel: 'noreferrer',
                style: { textDecoration: 'none' },
              },
              t('zoom.open'),
            ),
            h(
              'button',
              { type: 'button', className: 'photo-lightbox-btn', onClick: onClose },
              t('zoom.close'),
            ),
          ),
        ),
        h(
          'div',
          {
            className: 'photo-lightbox-stage',
            ref: stageRef,
            style: {
              cursor: zoomed ? (dragging ? 'grabbing' : 'grab') : 'zoom-in',
              touchAction: 'none',
              userSelect: 'none',
            },
            onPointerDown: startDrag,
            onPointerMove: moveDrag,
            onPointerUp: endDrag,
            onPointerCancel: endDrag,
            onLostPointerCapture: endDrag,
            onDoubleClick: (event) => {
              event.preventDefault();
              resetView();
            },
          },
          h('img', {
            src: image.src,
            alt: image.alt || image.title || '',
            className: 'photo-lightbox-image',
            ref: imgRef,
            style: imageStyle,
            draggable: false,
            onLoad: (event) => {
              const target = event.target;
              if (target && target.naturalWidth) {
                setNatural({ width: target.naturalWidth, height: target.naturalHeight });
              }
              resetView();
            },
          }),
        ),
        h('div', { className: 'photo-lightbox-foot' }, t('zoom.help')),
      );
    }

    function LabeledList({ title, items }) {
      if (!Array.isArray(items) || items.length === 0) return null;
      return h(
        'div',
        null,
        h('div', { className: 'photo-muted', style: { marginTop: 10 } }, title),
        h(
          'ul',
          { className: 'photo-list' },
          items.map((item, index) => h('li', { key: String(index) }, item)),
        ),
      );
    }

    function PhotoIcon({ size = 18, active = false }) {
      return h(
        'span',
        {
          className: 'photo-icon-wrap',
          style: {
            color: active
              ? 'var(--dsw-alias-brand-primary)'
              : 'var(--dsw-alias-label-secondary)',
            width: size,
            height: size,
          },
        },
        h(
          'svg',
          {
            width: size,
            height: size,
            viewBox: '0 0 24 24',
            fill: 'none',
            'aria-hidden': 'true',
          },
          h('circle', {
            cx: 12,
            cy: 12.4,
            r: 8.2,
            stroke: 'currentColor',
            strokeWidth: 1.8,
          }),
          h('path', {
            d: 'M12 4.6 12 12.4 18.2 8.9 M18.2 8.9 15 18.6 12 12.4 M15 18.6 5.8 18.6 12 12.4 M5.8 18.6 5.4 7.9 12 12.4 M5.4 7.9 12 4.6',
            stroke: 'currentColor',
            strokeWidth: 1.35,
            strokeLinecap: 'round',
            strokeLinejoin: 'round',
            opacity: 0.9,
          }),
          h('path', {
            d: 'M18.4 3.2 19.2 5.6 21.6 6.4 19.2 7.2 18.4 9.6 17.6 7.2 15.2 6.4 17.6 5.6 Z',
            fill: 'var(--dsw-alias-brand-primary)',
          }),
        ),
      );
    }

    class PanelBoundary extends React.Component {
      constructor(props) {
        super(props);
        this.state = { error: '' };
      }

      static getDerivedStateFromError(error) {
        return { error: error instanceof Error ? error.message : String(error) };
      }

      render() {
        if (this.state.error) {
          const { t } = this.props;
          const label = (typeof t === 'function' && t('error.panelRender')) || 'Panel render failed';
          return h(
            'div',
            { className: 'photo-root' },
            h('div', { className: 'photo-alert photo-alert-error' }, `${label}：${this.state.error}`),
          );
        }
        return this.props.children;
      }
    }

    function Card({ title, extra, children }) {
      return h(
        'section',
        { className: 'photo-card' },
        title || extra
          ? h(
              'div',
              { className: 'photo-card-head' },
              h('h2', { className: 'photo-card-title' }, title || ''),
              extra || null,
            )
          : null,
        children,
      );
    }

    function KeyValue({ rows }) {
      return h(
        'dl',
        { className: 'photo-kv' },
        rows.map(([label, value]) =>
          h(
            React.Fragment,
            { key: label },
            h('dt', null, label),
            h('dd', null, value === undefined || value === null || value === '' ? '—' : String(value)),
          ),
        ),
      );
    }

    function StyleCard({ style, index, active, onSelect, t }) {
      return h(
        'article',
        {
          className: `photo-style${active ? ' selected' : ''}`,
          onClick: () => onSelect(style.id),
          role: 'button',
          tabIndex: 0,
          onKeyDown: (event) => {
            if (event.key === 'Enter' || event.key === ' ') onSelect(style.id);
          },
          'aria-pressed': active,
        },
        h(
          'div',
          { className: 'photo-style-top' },
          h('div', { className: 'photo-style-name' }, style.name),
          index === 0 ? h('span', { className: 'photo-badge' }, t('styles.aiPick')) : null,
        ),
        h('div', { className: 'photo-style-desc' }, style.description),
        h(
          'div',
          { className: 'photo-style-field' },
          h('strong', null, `${t('styles.reason')}：`),
          style.best_fit_reason,
        ),
        h(
          'div',
          { className: 'photo-style-field' },
          h('strong', null, `${t('styles.impact')}：`),
          style.expected_visual_impact,
        ),
        h(
          'div',
          { className: 'photo-style-field' },
          h('strong', null, `${t('styles.rationale')}：`),
          style.rationale,
        ),
      );
    }

    function LightroomCard({ t, lightroom, busy, onRefresh, onInstall }) {
      const detected = lightroom && lightroom.detected;
      const installed = lightroom && lightroom.plugin_installed;
      return h(
        Card,
        {
          title: t('lightroom.title'),
          extra: h(
            'div',
            { className: 'photo-toolbar' },
            h(
              'button',
              { className: 'photo-btn', onClick: onRefresh, disabled: Boolean(busy) },
              t('action.refresh'),
            ),
            h(
              'button',
              { className: 'photo-btn', onClick: onInstall, disabled: Boolean(busy) },
              t('action.install'),
            ),
          ),
        },
        h(
          'div',
          { className: 'photo-chips' },
          h(
            'span',
            { className: `photo-chip ${detected ? 'photo-chip-ok' : 'photo-chip-warn'}` },
            `${t('status.lightroom')}：${detected ? t('status.detected') : t('status.notDetected')}`,
          ),
          h(
            'span',
            { className: `photo-chip ${installed ? 'photo-chip-ok' : 'photo-chip-warn'}` },
            installed ? t('status.pluginInstalled') : t('status.pluginMissing'),
          ),
        ),
        h('p', { className: 'photo-muted', style: { marginTop: 10 } }, lightroom?.message || t('lightroom.message')),
        lightroom?.presets_dir
          ? h('p', { className: 'photo-note' }, `${t('export.title')}：${lightroom.presets_dir}`)
          : null,
      );
    }

    function PhotoTab({ t, state, lightroom, busy, error, notice, onRefresh, onOpenFull, onReset, onLightroom }) {
      const [photo, setPhoto] = useState(null);
      const [analysis, setAnalysis] = useState(null);
      const [selectedStyle, setSelectedStyle] = useState('');
      const [intensity, setIntensity] = useState(1);
      const [preview, setPreview] = useState(null);
      const [exported, setExported] = useState(null);
      const [develop, setDevelop] = useState(null);
      const [localError, setLocalError] = useState('');
      const [localNotice, setLocalNotice] = useState('');
      const [uploadBusy, setUploadBusy] = useState(false);
      const [activeAction, setActiveAction] = useState('busy.analyze');
      const [elapsed, setElapsed] = useState(0);
      const lightbox = useLightbox();
      // Tracks the photo id whose cached selection was already applied, so a
      // later parent state refresh cannot overwrite a user-chosen style/intensity.
      const restoredPhotoIdRef = useRef('');

      const effectiveError = error || localError;
      const effectiveNotice = notice || localNotice;
      const isBusy = Boolean(busy) || uploadBusy;

      useEffect(() => {
        const last = state && state.last;
        if (!last || !last.photo_id) return;
        if (restoredPhotoIdRef.current === last.photo_id) return;
        restoredPhotoIdRef.current = last.photo_id;
        setPhoto({
          photo_id: last.photo_id,
          metadata: last.metadata || null,
          display_url: null,
        });
        setAnalysis(last.analysis || null);
        setSelectedStyle(
          last.style_id || (last.analysis?.recommended_styles?.[0]?.id || ''),
        );
        if (typeof last.intensity === 'number') setIntensity(last.intensity);
      }, [state]);

      useEffect(() => {
        if (!uploadBusy) return undefined;
        const started = Date.now();
        setElapsed(0);
        const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 500);
        return () => clearInterval(timer);
      }, [uploadBusy]);

      const displayPath = photo
        ? photo.display_url || `/api/photos/${photo.photo_id}/display`
        : '';
      const displayUrl = assetUrl(displayPath);
      const styles = (analysis && analysis.recommended_styles) || [];
      const selectedStyleData = styles.find((item) => item.id === selectedStyle) || null;

      async function handleFile(event) {
        const file = event.target.files && event.target.files[0];
        event.target.value = '';
        if (!file) return;
        const maxUploadBytes =
          Number(state && state.config && state.config.max_upload_bytes) || 0;
        if (maxUploadBytes > 0 && file.size > maxUploadBytes) {
          setLocalError(
            t('upload.tooLarge')
              .replace('{size}', (file.size / (1024 * 1024)).toFixed(1))
              .replace('{limit}', Math.round(maxUploadBytes / (1024 * 1024))),
          );
          return;
        }
        setLocalError('');
        setLocalNotice('');
        setAnalysis(null);
        setPreview(null);
        setExported(null);
        setDevelop(null);
        setSelectedStyle('');
        setActiveAction('busy.analyze');
        setUploadBusy(true);
        try {
          const uploaded = await uploadFile(file);
          setPhoto(uploaded);
          const analyzed = await postJson('/analyze', {
            photo_id: uploaded.photo_id,
            provider: 'default',
          });
          setAnalysis(analyzed.analysis);
          setSelectedStyle(analyzed.analysis.recommended_styles?.[0]?.id || '');
          setIntensity(1);
          setLocalNotice(t('notice.analyzed'));
        } catch (uploadError) {
          setLocalError(errorText(uploadError, t));
        } finally {
          setUploadBusy(false);
        }
      }

      async function retryAnalyze() {
        if (!photo) return;
        setActiveAction('busy.analyze');
        setUploadBusy(true);
        setLocalError('');
        try {
          const analyzed = await postJson('/analyze', {
            photo_id: photo.photo_id,
            provider: 'default',
          });
          setAnalysis(analyzed.analysis);
          setSelectedStyle(analyzed.analysis.recommended_styles?.[0]?.id || '');
          setLocalNotice(t('notice.analyzed'));
        } catch (analysisError) {
          setLocalError(errorText(analysisError, t));
        } finally {
          setUploadBusy(false);
        }
      }

      function chooseStyle(styleId) {
        setSelectedStyle(styleId);
        setPreview(null);
        setExported(null);
        setDevelop(null);
      }

      async function generatePreview() {
        if (!photo || !selectedStyle) return;
        setActiveAction('busy.preview');
        setUploadBusy(true);
        setLocalError('');
        try {
          const payload = await postJson('/preview', {
            photo_id: photo.photo_id,
            style_id: selectedStyle,
            intensity,
          });
          setPreview(payload.preview);
          setLocalNotice(t('notice.previewed'));
        } catch (previewError) {
          setLocalError(errorText(previewError, t));
        } finally {
          setUploadBusy(false);
        }
      }

      async function exportXmp() {
        if (!photo || !selectedStyle) return;
        setActiveAction('busy.export');
        setUploadBusy(true);
        setLocalError('');
        try {
          const payload = await postJson('/export', {
            photo_id: photo.photo_id,
            style_id: selectedStyle,
            intensity,
            action: 'xmp',
          });
          setExported(payload.export);
          setLocalNotice(t('notice.exported'));
        } catch (exportError) {
          setLocalError(errorText(exportError, t));
        } finally {
          setUploadBusy(false);
        }
      }

      async function generateDevelop() {
        if (!photo || !selectedStyle) return;
        setActiveAction('busy.develop');
        setUploadBusy(true);
        setLocalError('');
        try {
          const payload = await postJson('/export', {
            photo_id: photo.photo_id,
            style_id: selectedStyle,
            intensity,
            action: 'develop',
          });
          setDevelop(payload.develop);
          setLocalNotice(t('notice.developed'));
        } catch (developError) {
          setLocalError(errorText(developError, t));
        } finally {
          setUploadBusy(false);
        }
      }

      async function sendLightroom() {
        if (!photo || !selectedStyle) return;
        setActiveAction('busy.send');
        setUploadBusy(true);
        setLocalError('');
        try {
          const payload = await postJson('/lightroom', {
            action: 'send',
            photo_id: photo.photo_id,
            style_id: selectedStyle,
            intensity,
            install_plugin: true,
          });
          const result = payload.result || {};
          setLocalNotice(result.message || t('notice.sent'));
          onLightroom();
        } catch (sendError) {
          setLocalError(errorText(sendError, t));
        } finally {
          setUploadBusy(false);
        }
      }

      async function clearResult() {
        setPhoto(null);
        setAnalysis(null);
        setPreview(null);
        setExported(null);
        setDevelop(null);
        setSelectedStyle('');
        setLocalError('');
        setLocalNotice('');
        try {
          await onReset();
          setLocalNotice(t('notice.reset'));
        } catch (resetError) {
          setLocalError(errorText(resetError, t));
        }
      }

      const busyText = t(activeAction);

      return h(
        'div',
        null,
        effectiveError
          ? h('div', { className: 'photo-alert photo-alert-error' }, effectiveError)
          : null,
        effectiveNotice
          ? h('div', { className: 'photo-alert photo-alert-ok' }, effectiveNotice)
          : null,
        uploadBusy
          ? h('div', { className: 'photo-spinner' }, `${busyText || t('busy.analyze')}${elapsed ? ` ${elapsed}s` : ''}`)
          : null,

        h(
          Card,
          {
            title: t('upload.title'),
            extra: photo
              ? h(
                  'div',
                  { className: 'photo-toolbar' },
                  h(
                    'button',
                    { className: 'photo-btn', onClick: retryAnalyze, disabled: isBusy || !photo },
                    t('action.analyze'),
                  ),
                  h(
                    'button',
                    { className: 'photo-btn photo-btn-danger', onClick: clearResult, disabled: isBusy },
                    t('action.reset'),
                  ),
                )
              : null,
          },
          h(
            'label',
            { className: 'photo-file' },
            h('input', {
              type: 'file',
              accept: '.jpg,.jpeg,.png,.tif,.tiff,.cr3',
              onChange: handleFile,
              disabled: isBusy,
            }),
            h('span', { style: { marginLeft: 8 } }, photo ? t('upload.replace') : t('action.upload')),
          ),
          h('p', { className: 'photo-note' }, t('upload.hint')),
          photo && displayUrl
            ? h(
                'div',
                { className: 'photo-photo-line', style: { marginTop: 14 } },
                h(ZoomableImage, {
                  src: displayUrl,
                  alt: photo.metadata?.format || 'photo',
                  title: `${t('upload.title')}${
                    photo.metadata ? ` · ${photo.metadata.width} × ${photo.metadata.height}` : ''
                  }`,
                  onZoom: lightbox.open,
                  t,
                  className: 'photo-thumb-wrap',
                  imageClassName: 'photo-thumb',
                }),
                h(
                  'div',
                  { className: 'photo-photo-meta' },
                  h(KeyValue, {
                    rows: [
                      ['ID', photo.photo_id],
                      [t('photo.size'), photo.metadata ? `${photo.metadata.width} × ${photo.metadata.height}` : '—'],
                      [t('photo.format'), photo.metadata?.format || '—'],
                      [t('photo.renderSource'), photo.metadata?.render_source || '—'],
                    ],
                  }),
                  h('p', { className: 'photo-note' }, t('zoom.hint')),
                ),
              )
            : null,
        ),

        analysis
          ? h(
              Card,
              { title: t('analysis.title') },
              h('p', { className: 'photo-note', style: { marginTop: 0 } }, t('analysis.byAi')),
              h('p', { className: 'photo-muted', style: { marginTop: 10 } }, analysis.overall_summary),
              analysis.series && analysis.series.note
                ? h('p', { className: 'photo-note' }, analysis.series.note)
                : null,
              h(LabeledList, { title: t('analysis.technical'), items: analysis.technical_diagnosis }),
              h(LabeledList, { title: t('analysis.composition'), items: analysis.composition_observations }),
              h(LabeledList, { title: t('analysis.color'), items: analysis.color_tone_observations }),
            )
          : null,

        analysis && styles.length > 0
          ? h(
              Card,
              { title: t('styles.title') },
              h(
                'div',
                { className: 'photo-grid' },
                styles.map((style, index) =>
                  h(StyleCard, {
                    key: style.id,
                    style,
                    index,
                    active: style.id === selectedStyle,
                    onSelect: chooseStyle,
                    t,
                  }),
                ),
              ),
            )
          : null,

        analysis && selectedStyleData
          ? h(
              Card,
              { title: t('preview.title') },
              h(
                'div',
                { className: 'photo-slider-row' },
                h('span', { className: 'photo-muted' }, `${t('preview.intensity')} ${percent(intensity)}`),
                h('input', {
                  className: 'photo-slider',
                  type: 'range',
                  min: 0,
                  max: 1,
                  step: 0.05,
                  value: intensity,
                  onChange: (event) => {
                    setIntensity(Number(event.target.value));
                    setPreview(null);
                  },
                }),
              ),
              h(
                'div',
                { className: 'photo-actions', style: { marginTop: 12 } },
                h(
                  'button',
                  { className: 'photo-btn photo-btn-primary', onClick: generatePreview, disabled: isBusy },
                  t('action.preview'),
                ),
                h(
                  'button',
                  { className: 'photo-btn', onClick: exportXmp, disabled: isBusy },
                  t('action.export'),
                ),
                h(
                  'button',
                  { className: 'photo-btn', onClick: generateDevelop, disabled: isBusy },
                  t('action.develop'),
                ),
                h(
                  'button',
                  { className: 'photo-btn', onClick: sendLightroom, disabled: isBusy },
                  t('action.send'),
                ),
              ),
              h(
                'div',
                { className: 'photo-compare' },
                h(
                  'div',
                  { className: 'photo-compare-cell' },
                  h('div', { className: 'photo-compare-label' }, t('preview.original')),
                  displayUrl
                    ? h(ZoomableImage, {
                        src: displayUrl,
                        alt: t('preview.original'),
                        title: t('preview.original'),
                        onZoom: lightbox.open,
                        t,
                      })
                    : h('div', { className: 'photo-muted', style: { padding: 12 } }, '—'),
                ),
                h(
                  'div',
                  { className: 'photo-compare-cell' },
                  h('div', { className: 'photo-compare-label' }, t('preview.approximate')),
                  preview?.preview_url
                    ? h(ZoomableImage, {
                        src: assetUrl(preview.preview_url),
                        alt: t('preview.approximate'),
                        title: `${t('preview.approximate')} · ${percent(intensity)}`,
                        onZoom: lightbox.open,
                        t,
                      })
                    : h(
                        'div',
                        { className: 'photo-muted', style: { padding: 12 } },
                        t('preview.empty'),
                      ),
                ),
              ),
              h('p', { className: 'photo-note' }, t('zoom.hint')),
              h('p', { className: 'photo-note' }, t('preview.note')),
            )
          : null,

        exported
          ? h(
              Card,
              { title: t('export.title') },
              h(KeyValue, {
                rows: [
                  [t('export.preset'), exported.preset_name],
                  [t('export.mapping'), exported.mapping_version],
                ],
              }),
              preview?.preview_url
                ? h(ZoomableImage, {
                    src: assetUrl(preview.preview_url),
                    alt: t('preview.approximate'),
                    title: `${t('preview.approximate')} · ${percent(intensity)}`,
                    onZoom: lightbox.open,
                    t,
                    buttonStyle: { maxWidth: 460, margin: '12px auto 0', borderRadius: 8 },
                    imageClassName: 'photo-export-thumb',
                  })
                : null,
              h(
                'a',
                {
                  className: 'photo-btn',
                  href: assetUrl(exported.download_url),
                  target: '_blank',
                  rel: 'noreferrer',
                  style: { marginTop: 10, textDecoration: 'none' },
                },
                t('export.download'),
              ),
            )
          : null,

        develop
          ? h(
              Card,
              { title: t('develop.title') },
              h(KeyValue, {
                rows: [
                  [t('export.preset'), develop.preset_name],
                  [t('export.mapping'), develop.mapping_version],
                  [t('develop.key'), ''],
                ],
              }),
              h(
                'table',
                { className: 'photo-table' },
                h(
                  'tbody',
                  null,
                  [
                    [t('develop.exposure'), develop.parameters?.exposure],
                    [t('develop.contrast'), develop.parameters?.contrast],
                    [t('develop.highlights'), develop.parameters?.highlights],
                    [t('develop.shadows'), develop.parameters?.shadows],
                    [t('develop.temperature'), develop.parameters?.temperature],
                    [t('develop.vibrance'), develop.parameters?.vibrance],
                    [t('develop.clarity'), develop.parameters?.clarity],
                  ].map(([label, value]) =>
                    h(
                      'tr',
                      { key: label },
                      h('th', null, label),
                      h('td', null, value === undefined ? '—' : String(value)),
                    ),
                  ),
                ),
              ),
              h(
                'a',
                {
                  className: 'photo-btn',
                  href: assetUrl(develop.xmp_url),
                  target: '_blank',
                  rel: 'noreferrer',
                  style: { marginTop: 10, textDecoration: 'none' },
                },
                t('export.download'),
              ),
            )
          : null,

        h(LightroomCard, {
          t,
          lightroom,
          busy: isBusy,
          onRefresh: onLightroom,
          onInstall: async () => {
            setActiveAction('busy.install');
            setUploadBusy(true);
            setLocalError('');
            try {
              const payload = await postJson('/lightroom', { action: 'install' });
              setLocalNotice(payload.install?.message || t('notice.installed'));
              onLightroom();
            } catch (installError) {
              setLocalError(errorText(installError, t));
            } finally {
              setUploadBusy(false);
            }
          },
        }),

        h(
          'div',
          { className: 'photo-toolbar', style: { marginTop: 6 } },
          h(
            'button',
            { className: 'photo-btn', onClick: onOpenFull },
            t('action.openFull'),
          ),
        ),
        h('p', { className: 'photo-footer' }, t('footer.privacy')),
        h(Lightbox, { image: lightbox.image, onClose: lightbox.close, t }),
      );
    }

    function KnowledgeTab({ t, state }) {
      const [overview, setOverview] = useState(null);
      const [list, setList] = useState(null);
      const [filter, setFilter] = useState({ dimension: '', tag: '', category: '' });
      const [loading, setLoading] = useState(false);
      const [error, setError] = useState('');
      const [notice, setNotice] = useState('');
      const [details, setDetails] = useState({});
      const [pendingId, setPendingId] = useState('');
      const lightbox = useLightbox();
      const loadSequence = useRef(0);

      async function load(nextFilter) {
        const active = nextFilter || filter;
        const sequence = loadSequence.current + 1;
        loadSequence.current = sequence;
        setLoading(true);
        try {
          const query = new URLSearchParams({ limit: '50', offset: '0' });
          if (active.tag) {
            query.set('tag', active.tag);
            if (active.dimension) query.set('dimension', active.dimension);
          }
          if (active.category) query.set('category', active.category);
          const [overviewPayload, listPayload] = await Promise.all([
            api('/knowledge/overview'),
            api(`/knowledge/summaries?${query.toString()}`),
          ]);
          if (sequence !== loadSequence.current) return;
          setOverview(overviewPayload);
          setList(listPayload);
          setError('');
        } catch (loadError) {
          if (sequence !== loadSequence.current) return;
          setError(errorText(loadError, t));
        } finally {
          if (sequence === loadSequence.current) setLoading(false);
        }
      }

      useEffect(() => {
        void load();
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, []);

      function selectFilter(nextFilter) {
        setFilter(nextFilter);
        void load(nextFilter);
      }

      async function hydrateDetail(photoId) {
        if (details[photoId]) return;
        setDetails((current) => ({ ...current, [photoId]: 'loading' }));
        try {
          const payload = await api(`/knowledge/summaries/${photoId}`);
          setDetails((current) => ({ ...current, [photoId]: payload.summary }));
        } catch (detailError) {
          setDetails((current) => ({ ...current, [photoId]: { error: errorText(detailError, t) } }));
        }
      }

      async function rebuild(photoId) {
        setPendingId(photoId);
        setError('');
        setNotice('');
        try {
          const payload = await postJson(`/knowledge/summaries/${photoId}/rebuild`, {});
          setDetails((current) => ({ ...current, [photoId]: payload.summary }));
          setNotice(t('knowledge.rebuilt'));
          await load();
        } catch (rebuildError) {
          setError(errorText(rebuildError, t));
        } finally {
          setPendingId('');
        }
      }

      function filterLabel() {
        const parts = [];
        if (filter.category) parts.push(filter.category);
        if (filter.tag) parts.push(filter.tag);
        return parts.join(' · ');
      }

      function renderDirection(direction) {
        return h(
          'div',
          { className: 'photo-knowledge-direction', key: direction.style_id },
          h(
            'div',
            { className: 'photo-knowledge-direction-head' },
            h('span', { className: 'photo-knowledge-direction-name' }, direction.style_name),
            h(
              'span',
              { className: 'photo-chip photo-chip-ok' },
              `${t('knowledge.intensity')} ${percent(direction.recommended_intensity)}`,
            ),
          ),
          direction.direction
            ? h('p', { className: 'photo-knowledge-direction-note' }, direction.direction)
            : null,
          direction.intensity_reason
            ? h('p', { className: 'photo-knowledge-direction-note' }, direction.intensity_reason)
            : null,
          direction.parameters_summary
            ? h(
                'p',
                { className: 'photo-knowledge-direction-note' },
                `${t('knowledge.parameters')}：${direction.parameters_summary}`,
              )
            : null,
          direction.preview_url
            ? h(ZoomableImage, {
                src: assetUrl(direction.preview_url),
                alt: `${direction.style_name} ${t('knowledge.preview')}`,
                title: `${direction.style_name} · ${t('knowledge.preview')} · ${percent(
                  direction.preview_intensity == null ? direction.recommended_intensity : direction.preview_intensity,
                )}`,
                onZoom: lightbox.open,
                t,
                imageClassName: 'photo-knowledge-preview',
              })
            : null,
          (direction.warnings || []).length
            ? h(
                'ul',
                { className: 'photo-list' },
                (direction.warnings || []).map((warning, index) => h('li', { key: String(index) }, warning)),
              )
            : null,
        );
      }

      function renderCard(item) {
        const detail = details[item.photo_id];
        const summary = detail && detail.summary ? detail.summary : null;
        const imageUrl = item.image && item.image.display_url ? assetUrl(item.image.display_url) : '';
        const styleNames = item.style_names || [];
        const intensities = item.recommended_intensities || [];
        return h(
          'article',
          { className: 'photo-knowledge-card', key: item.photo_id },
          h(
            'div',
            { className: 'photo-knowledge-card-head' },
            h('span', { className: 'photo-badge' }, item.primary_category || '—'),
            h('span', { className: 'photo-muted' }, formatKnowledgeTime(item.generated_at)),
          ),
          imageUrl
            ? h(ZoomableImage, {
                src: imageUrl,
                alt: item.overall_summary || item.photo_id,
                title: `${item.primary_category || ''} · ${formatKnowledgeTime(item.generated_at)}`,
                onZoom: lightbox.open,
                t,
                imageClassName: 'photo-knowledge-thumb',
              })
            : null,
          (item.tags || []).length
            ? h(
                'div',
                { className: 'photo-knowledge-tags' },
                (item.tags || []).map((tag) =>
                  h('span', { className: 'photo-chip', key: `${tag.dimension}-${tag.label}` }, tag.label),
                ),
              )
            : null,
          h('p', { className: 'photo-knowledge-summary' }, item.overall_summary || ''),
          styleNames.length ? h('p', { className: 'photo-muted' }, styleNames.join(' · ')) : null,
          intensities.length
            ? h(
                'p',
                { className: 'photo-muted' },
                `${t('knowledge.intensity')}：${intensities.map(percent).join(' / ')}`,
              )
            : null,
          detail === 'loading'
            ? h('p', { className: 'photo-spinner' }, t('knowledge.loadingDirections'))
            : detail && detail.error
              ? h('div', { className: 'photo-alert photo-alert-error' }, detail.error)
              : summary
                ? h('div', null, summary.directions.map(renderDirection))
                : h(
                    'button',
                    { className: 'photo-btn', onClick: () => hydrateDetail(item.photo_id) },
                    t('knowledge.directions'),
                  ),
        );
      }

      const items = (list && list.items) || [];
      const categories = (list && list.primary_categories) || (overview && overview.primary_categories) || [];
      const facets = (list && list.facets) || (overview && overview.facets) || [];
      const last = state && state.last;
      const lastPhotoId = last && last.photo_id;

      return h(
        'div',
        null,
        error ? h('div', { className: 'photo-alert photo-alert-error' }, error) : null,
        notice ? h('div', { className: 'photo-alert photo-alert-ok' }, notice) : null,
        h(
          Card,
          {
            title: t('knowledge.title'),
            extra: h(
              'div',
              { className: 'photo-actions' },
              lastPhotoId
                ? h(
                    'button',
                    {
                      className: 'photo-btn photo-btn-primary',
                      onClick: () => rebuild(lastPhotoId),
                      disabled: Boolean(pendingId) || loading,
                    },
                    t('knowledge.rebuild'),
                  )
                : h('span', { className: 'photo-muted' }, t('knowledge.needPhoto')),
              h(
                'button',
                { className: 'photo-btn', onClick: () => load(), disabled: loading },
                loading ? t('busy.knowledge') : t('knowledge.refresh'),
              ),
            ),
          },
          overview
            ? h(
                'div',
                { className: 'photo-knowledge-stats' },
                h(
                  'span',
                  { className: 'photo-chip' },
                  `${t('knowledge.statsCount')} ${overview.total_summaries}`,
                ),
                h(
                  'span',
                  { className: 'photo-chip' },
                  `${t('knowledge.statsCategories')} ${overview.covered_categories}`,
                ),
                h(
                  'span',
                  { className: 'photo-chip' },
                  `${t('knowledge.statsIntensity')} ${
                    overview.average_intensity == null ? '—' : percent(overview.average_intensity)
                  }`,
                ),
              )
            : null,
          categories.length
            ? h(
                'div',
                { className: 'photo-knowledge-group' },
                h('span', { className: 'photo-knowledge-group-label' }, t('knowledge.categories')),
                categories.map((item) =>
                  h(
                    'button',
                    {
                      key: item.category,
                      type: 'button',
                      className: 'photo-chip photo-chip-btn',
                      'aria-pressed': filter.category === item.category,
                      onClick: () =>
                        selectFilter({
                          ...filter,
                          category: filter.category === item.category ? '' : item.category,
                        }),
                    },
                    `${item.category} ${item.count}`,
                  ),
                ),
              )
            : null,
          facets.map((facet) =>
            h(
              'div',
              { className: 'photo-knowledge-group', key: facet.dimension },
              h('span', { className: 'photo-knowledge-group-label' }, `${t('knowledge.tags')} · ${facet.label}`),
              facet.tags.map((tag) => {
                const active = filter.tag === tag.label && filter.dimension === facet.dimension;
                return h(
                  'button',
                  {
                    key: `${facet.dimension}-${tag.label}`,
                    type: 'button',
                    className: 'photo-chip photo-chip-btn',
                    'aria-pressed': active,
                    onClick: () =>
                      selectFilter(
                        active
                          ? { ...filter, tag: '', dimension: '' }
                          : { ...filter, tag: tag.label, dimension: facet.dimension },
                      ),
                  },
                  `${tag.label} ${tag.count}`,
                );
              }),
            ),
          ),
          filter.category || filter.tag
            ? h(
                'div',
                { className: 'photo-knowledge-toolbar' },
                h('span', { className: 'photo-muted' }, `${t('knowledge.filtered')}：${filterLabel()}`),
                h(
                  'button',
                  {
                    className: 'photo-btn',
                    onClick: () => selectFilter({ dimension: '', tag: '', category: '' }),
                  },
                  t('knowledge.clearFilters'),
                ),
              )
            : null,
          items.length
            ? h('div', { className: 'photo-knowledge-grid', style: { marginTop: 12 } }, items.map(renderCard))
            : h(
                'p',
                { className: 'photo-knowledge-empty' },
                loading ? t('busy.knowledge') : t('knowledge.empty'),
              ),
        ),
        h(Lightbox, { image: lightbox.image, onClose: lightbox.close, t }),
      );
    }

    function StatusTab({ t, state, lightroom, busy, onRefresh, onOpenFull, onReset, onInstall }) {
      const config = (state && state.config) || {};
      const backend = (state && state.backend) || {};
      return h(
        'div',
        null,
        h(
          Card,
          {
            title: t('status.backend'),
            extra: h(
              'div',
              { className: 'photo-toolbar' },
              h('button', { className: 'photo-btn', onClick: onRefresh }, t('action.refresh')),
              h('button', { className: 'photo-btn', onClick: onOpenFull }, t('action.openFull')),
              h('button', { className: 'photo-btn photo-btn-danger', onClick: onReset }, t('action.reset')),
            ),
          },
          h(
            'div',
            { className: 'photo-chips' },
            h(
              'span',
              { className: `photo-chip ${backend.ready ? 'photo-chip-ok' : 'photo-chip-warn'}` },
              `${t('status.backend')}：${backend.ready ? t('status.ready') : busy ? t('status.starting') : t('status.notRunning')}`,
            ),
            h(
              'span',
              { className: 'photo-chip' },
              `${t('status.port')} ${config.port || '—'}`,
            ),
            h(
              'span',
              { className: 'photo-chip' },
              `${t('status.provider')} ${config.provider || '—'}`,
            ),
            h(
              'span',
              { className: 'photo-chip' },
              backend.managed ? t('status.managed') : backend.external ? t('status.external') : '—',
            ),
          ),
          h(KeyValue, {
            rows: [
              [t('status.python'), config.python],
              [t('status.dataDir'), config.data_root],
              ['Base URL', config.base_url],
            ],
          }),
          backend.last_error
            ? h('div', { className: 'photo-alert photo-alert-error', style: { marginTop: 10 } }, backend.last_error)
            : null,
          h('div', { className: 'photo-muted', style: { marginTop: 12 } }, t('status.log')),
          h('pre', { className: 'photo-pre' }, backend.log_tail || t('status.noLog')),
        ),
        h(LightroomCard, {
          t,
          lightroom,
          busy,
          onRefresh,
          onInstall,
        }),
      );
    }

    function PhotoPanel({ usePanelInfo }) {
      void usePanelInfo;
      const { t } = useDictionary();
      const [state, setState] = useState(null);
      const [lightroom, setLightroom] = useState(null);
      const [busy, setBusy] = useState('ensure');
      const [error, setError] = useState('');
      const [notice, setNotice] = useState('');
      const [tab, setTab] = useState('photo');
      // Lazy keep-alive: once the photo tab has been mounted, keep its state
      // (photo/analysis/style/intensity/preview/export/develop) alive while the
      // other tabs are shown; switching back must not refetch or reset.
      const [photoVisited, setPhotoVisited] = useState(false);

      useEffect(() => {
        if (tab === 'photo') setPhotoVisited(true);
      }, [tab]);

      const refresh = async () => {
        setBusy('ensure');
        setError('');
        try {
          const payload = await postJson('/ensure', {});
          setState({
            config: payload.config,
            backend: payload.backend,
            last: (await api('/state')).last,
          });
          setLightroom(payload.lightroom);
          if (payload.lightroom_error) setError(payload.lightroom_error);
        } catch (ensureError) {
          setError(errorText(ensureError, t));
          try {
            const payload = await api('/state');
            setState(payload);
          } catch {
            /* keep the error */
          }
        } finally {
          setBusy('');
        }
      };

      useEffect(() => {
        let alive = true;
        (async () => {
          try {
            const first = await api('/state');
            if (alive) setState(first);
            const payload = await postJson('/ensure', {});
            if (!alive) return;
            const latest = await api('/state');
            if (!alive) return;
            setState(latest);
            setLightroom(payload.lightroom || null);
            if (payload.lightroom_error) setError(payload.lightroom_error);
          } catch (ensureError) {
            if (alive) {
              setError(errorText(ensureError, t));
              try {
                const payload = await api('/state');
                if (alive) setState(payload);
              } catch {
                /* keep the error */
              }
            }
          } finally {
            if (alive) setBusy('');
          }
        })();
        return () => {
          alive = false;
        };
      }, []);

      const openFull = () => {
        const base = (state && state.config && state.config.base_url) || 'http://127.0.0.1:8000';
        window.open(base, '_blank', 'noopener');
      };

      const refreshLightroom = async () => {
        setBusy('lightroom');
        setError('');
        try {
          const payload = await postJson('/lightroom', { action: 'status' });
          setLightroom(payload.lightroom);
          setNotice('');
        } catch (lightroomError) {
          setError(errorText(lightroomError, t));
        } finally {
          setBusy('');
        }
      };

      const reset = async () => {
        await postJson('/state/reset', {});
        setState((previous) => (previous ? { ...previous, last: null } : previous));
        setNotice(t('notice.reset'));
      };

      const installLightroom = async () => {
        setBusy('lightroom');
        setError('');
        try {
          const payload = await postJson('/lightroom', { action: 'install' });
          setNotice(payload.install?.message || t('notice.installed'));
          const statusPayload = await postJson('/lightroom', { action: 'status' });
          setLightroom(statusPayload.lightroom);
        } catch (installError) {
          setError(errorText(installError, t));
        } finally {
          setBusy('');
        }
      };

      const backendReady = Boolean(state && state.backend && state.backend.ready);
      const lightroomDetected = Boolean(lightroom && lightroom.detected);

      return h(
        'div',
        { className: 'photo-root' },
        h(
          'header',
          { className: 'photo-head' },
          h(
            'div',
            { className: 'photo-title-row' },
            h(
              'div',
              null,
              h('h1', { className: 'photo-title' }, t('panel.title')),
              h('p', { className: 'photo-subtitle' }, `${t('panel.subtitle')} · v${UI_BUILD}`),
            ),
            h(
              'div',
              { className: 'photo-chips' },
              h(
                'span',
                { className: `photo-chip ${backendReady ? 'photo-chip-ok' : 'photo-chip-warn'}` },
                `${t('status.backend')}：${backendReady ? t('status.ready') : busy ? t('status.starting') : t('status.notRunning')}`,
              ),
              h(
                'span',
                { className: `photo-chip ${lightroomDetected ? 'photo-chip-ok' : 'photo-chip-warn'}` },
                `${t('status.lightroom')}：${lightroomDetected ? t('status.detected') : t('status.notDetected')}`,
              ),
              h(
                'span',
                { className: 'photo-chip' },
                `${t('status.provider')} ${(state && state.config && state.config.provider) || '—'}`,
              ),
            ),
          ),
          error ? h('div', { className: 'photo-alert photo-alert-error' }, error) : null,
          notice ? h('div', { className: 'photo-alert photo-alert-ok' }, notice) : null,
          busy && busy !== 'ensure'
            ? h('div', { className: 'photo-spinner' }, t(`busy.${busy}`))
            : null,
        ),
        h(
          'nav',
          { className: 'photo-tabs', role: 'tablist' },
          ['photo', 'knowledge', 'status'].map((key) =>
            h(
              'button',
              {
                key,
                role: 'tab',
                className: `photo-tab${tab === key ? ' active' : ''}`,
                'aria-selected': tab === key,
                onClick: () => setTab(key),
              },
              t(`tab.${key}`),
            ),
          ),
        ),
        tab === 'photo' || photoVisited
          ? h(
              'div',
              {
                className: 'photo-tab-panel',
                style: tab === 'photo' ? undefined : { display: 'none' },
                hidden: tab !== 'photo',
                'aria-hidden': tab !== 'photo',
              },
              h(PhotoTab, {
                t,
                state,
                lightroom,
                busy: busy === 'ensure' ? '' : busy,
                error,
                notice,
                onRefresh: refresh,
                onOpenFull: openFull,
                onReset: reset,
                onLightroom: refreshLightroom,
              }),
            )
          : null,
        tab === 'knowledge' ? h(KnowledgeTab, { t, state }) : null,
        tab === 'status'
          ? h(StatusTab, {
              t,
              state,
              lightroom,
              busy,
              onRefresh: refresh,
              onOpenFull: openFull,
              onReset: reset,
              onInstall: installLightroom,
            })
          : null,
      );
    }

    function PhotoPanelBoundary(props) {
      const { t } = useDictionary();
      return h(PanelBoundary, { t }, h(PhotoPanel, props));
    }

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        localeService = ctx.locale;
        ctx.effect(() => ctx.locale.register(NS, DICT), 'photo-agent: dictionaries');
        ctx.effect(() => styles.insert(CSS), 'photo-agent: stylesheet');

        const t = ctx.locale.bind(NS);

        ctx.slots.inject('sidebar.panellist', () =>
          ctx.slots.register(
            {
              name: 'sidebar.panellist',
              id: 'photo-agent',
              order: 30,
              label: () => t('panel.title'),
            },
            PhotoIcon,
          ),
        );

        ctx.slots.inject('main', () =>
          ctx.slots.register(
            {
              name: 'main',
              key: 'photo-agent',
            },
            PhotoPanelBoundary,
          ),
        );
      },
    };
  },
});
