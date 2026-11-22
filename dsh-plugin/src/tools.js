/**
 * Agent-facing tools for the AI Photo Agent plugin.
 *
 * Tools call the same PhotoService methods as the panel routes. Parameter rules
 * live on the parameters; result text stays short and points at files or IDs
 * for follow-up calls.
 *
 * @module @local/dsh-photo-agent/tools
 */

import fs from 'node:fs';
import path from 'node:path';
import { PhotoError, errorMessage } from './errors.js';
import { contentTypeFor } from './service.js';

const IMAGE_EXTENSIONS = new Set(['.jpg', '.jpeg', '.png', '.tif', '.tiff', '.cr3']);

function text(value) {
  return [{ type: 'text', text: value }];
}

function output(renderer) {
  return { schema: {}, render: renderer };
}

function toolError(error) {
  if (error instanceof PhotoError) return error.message;
  return errorMessage(error);
}

function describeStyle(style, index) {
  const pick = index === 0 ? '（AI 首选）' : '';
  return `${index + 1}. ${style.id} ${style.name}${pick}\n   ${style.best_fit_reason}`;
}

function bulletList(items, limit = 4) {
  return (Array.isArray(items) ? items : [])
    .slice(0, limit)
    .map((item) => `- ${item}`)
    .join('\n');
}

function parametersLine(parameters) {
  if (!parameters) return '';
  const fields = [
    ['exposure', '曝光'],
    ['contrast', '对比'],
    ['highlights', '高光'],
    ['shadows', '阴影'],
    ['temperature', '色温'],
    ['vibrance', '鲜艳度'],
    ['clarity', '清晰度'],
    ['dehaze', '去雾'],
  ];
  return fields
    .filter(([key]) => parameters[key] !== undefined)
    .map(([key, label]) => `${label} ${parameters[key]}`)
    .join('，');
}

async function resolveImagePath(ctx, exec, raw) {
  const input = String(raw || '').trim();
  if (!input) {
    throw new Error('path 不能为空。请提供本地图片绝对路径（推荐），或使用 photo_id。');
  }
  if (path.isAbsolute(input)) return path.normalize(input);
  let cwd = process.cwd();
  try {
    const workingDirectory = ctx.get('workingDirectory');
    if (workingDirectory && exec && exec.agent) {
      const resolved = await workingDirectory.ensure(exec.agent);
      if (resolved) cwd = resolved;
    }
  } catch {
    /* fall back to process cwd */
  }
  return path.resolve(cwd, input);
}

async function readLocalImage(filePath, maxBytes) {
  let stat;
  try {
    stat = await fs.promises.stat(filePath);
  } catch {
    throw new Error(`找不到图片文件：${filePath}`);
  }
  if (!stat.isFile()) throw new Error(`不是文件：${filePath}`);
  if (stat.size <= 0) throw new Error(`图片文件为空：${filePath}`);
  if (stat.size > maxBytes) {
    throw new Error(`图片超过 ${Math.round(maxBytes / (1024 * 1024))} MB 上限：${filePath}`);
  }
  const extension = path.extname(filePath).toLowerCase();
  if (!IMAGE_EXTENSIONS.has(extension)) {
    throw new Error('只支持 JPG/JPEG、PNG、TIF/TIFF 和 Canon CR3 文件。');
  }
  try {
    return await fs.promises.readFile(filePath);
  } catch (error) {
    throw new Error(`读取图片失败：${errorMessage(error)}`);
  }
}

function readConfirm(args) {
  return Boolean(args && args.confirm === true);
}

/**
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {{
 *   service: ReturnType<import('./service.js').createPhotoService>,
 *   config: ReturnType<import('./config.js').loadConfig>,
 *   logger?: { info?: Function, warn?: Function },
 * }} deps
 */
export function registerTools(ctx, { service, config }) {
  const disposers = [];

  disposers.push(
    ctx.tools.register({
      name: 'photo_agent_analyze',
      description:
        '上传本地照片或复用已有 photo_id，调用本机 AI Photo Agent 做视觉诊断，返回总体诊断、技术/构图/色彩观察和恰好 3 套 Lightroom 修图风格。黑白只在画面适合时出现；分析会参考同系列照片，尽量保持一组照片的风格方向一致。用户要求分析照片、给修图建议时使用。分析通常 20-60 秒；成功后可用 photo_agent_preview 查看近似预览。',
      parameters: {
        type: 'object',
        properties: {
          path: {
            type: 'string',
            description:
              '本地图片绝对路径（推荐）或相对当前会话工作目录的路径；支持 .jpg/.jpeg/.png/.tif/.tiff/.cr3。与 photo_id 二选一。',
          },
          photo_id: {
            type: 'string',
            description: '已有照片的 32 位十六进制 ID；与 path 二选一，用于重试分析或继续操作。',
          },
          provider: {
            type: 'string',
            enum: ['default', 'deepseek', 'mock', 'openai', 'codex'],
            description: '分析 Provider；默认 default（使用后端配置，通常是 deepseek）。mock 仅用于离线验证。',
          },
        },
        required: [],
      },
      output: output((_args, value) => {
        const analysis = value.analysis || {};
        const styles = Array.isArray(analysis.recommended_styles) ? analysis.recommended_styles : [];
        const meta = value.metadata || {};
        const source = meta.render_source === 'embedded_preview' ? '，使用内嵌预览' : '';
        const described =
          meta.width && meta.height
            ? `${meta.width}×${meta.height}（${meta.format || '图片'}${source}）`
            : '照片';
        return text(
          [
            `已分析 ${described}。`,
            `诊断：${analysis.overall_summary || '—'}`,
            `技术：\n${bulletList(analysis.technical_diagnosis, 3)}`,
            `构图：\n${bulletList(analysis.composition_observations, 3)}`,
            `色彩：\n${bulletList(analysis.color_tone_observations, 3)}`,
            `风格：\n${styles.map(describeStyle).join('\n')}`,
            `photo_id=${value.photo_id}；可继续调用 photo_agent_preview 或 photo_agent_export。`,
          ].join('\n\n'),
        );
      }),
      timeoutMs: config.analysisTimeoutMs,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const rawPath = args && args.path;
        const photoId = args && args.photo_id;
        if (!rawPath && !photoId) {
          throw new Error('请提供 path 或 photo_id。');
        }
        if (rawPath && photoId) {
          throw new Error('path 与 photo_id 只能二选一。');
        }
        const provider = (args && args.provider) || 'default';
        if (photoId) {
          const analysis = await service.analyze(photoId, provider);
          const previous = service.store.read();
          return {
            photo_id: photoId,
            metadata: previous && previous.photo_id === photoId ? previous.metadata : null,
            analysis,
            provider,
            display_url: null,
          };
        }
        const filePath = await resolveImagePath(ctx, exec, rawPath);
        const data = await readLocalImage(filePath, config.maxUploadBytes);
        const uploaded = await service.uploadImage({
          data,
          filename: path.basename(filePath),
          contentType: contentTypeFor(filePath),
        });
        let analysis;
        try {
          analysis = await service.analyze(uploaded.photo_id, provider);
        } catch (error) {
          throw new Error(
            `照片已上传（photo_id=${uploaded.photo_id}），但分析失败：${toolError(error)}。可稍后只带 photo_id 重新分析。`,
          );
        }
        return {
          photo_id: uploaded.photo_id,
          metadata: uploaded.metadata,
          analysis,
          provider,
          display_url: uploaded.display_url,
          uploaded_from: filePath,
        };
      },
    }),
  );

  disposers.push(
    ctx.tools.register({
      name: 'photo_agent_preview',
      description:
        '为已分析照片的某一套风格生成 Pillow 近似预览，返回预览文件绝对路径、代理 URL 和缩放后的参数。用户想先看效果时使用；预览不等同于 Lightroom 渲染。',
      parameters: {
        type: 'object',
        properties: {
          photo_id: { type: 'string', description: '已分析照片的 32 位十六进制 ID。' },
          style_id: { type: 'string', description: 'photo_agent_analyze 返回的 3 个 style id 之一。' },
          intensity: {
            type: 'number',
            description: '风格强度，0–1，默认 0.7（70%）。0 表示接近原图，1 表示完整参数。',
          },
        },
        required: ['photo_id', 'style_id'],
      },
      output: output((_args, value) =>
        text(
          [
            `已生成近似预览（强度 ${Math.round((value.intensity ?? 0.7) * 100)}%，非 Lightroom 渲染）。`,
            `风格：${value.style_id}`,
            `文件：${value.preview_path || '（预览 URL 未映射到本地路径）'}`,
            `参数：${parametersLine(value.parameters) || '—'}`,
            '需要查看图像时，调用 read_image 读取上面的文件路径。',
          ].join('\n'),
        ),
      ),
      timeoutMs: 120_000,
      isConcurrencySafe: () => false,
      async execute(args) {
        return service.preview(args && args.photo_id, args && args.style_id, args && args.intensity);
      },
    }),
  );

  disposers.push(
    ctx.tools.register({
      name: 'photo_agent_export',
      description:
        '为已分析照片的某一套风格生成 Lightroom XMP 预设（action=xmp）或 Lightroom 显影参数（action=develop）。用户要导出预设、导入 Lightroom 或查看具体参数时使用。',
      parameters: {
        type: 'object',
        properties: {
          photo_id: { type: 'string', description: '已分析照片的 32 位十六进制 ID。' },
          style_id: { type: 'string', description: 'photo_agent_analyze 返回的 3 个 style id 之一。' },
          intensity: { type: 'number', description: '风格强度，0–1，默认 0.7（70%）。' },
          action: {
            type: 'string',
            enum: ['xmp', 'develop'],
            description: 'xmp 生成预设文件（默认）；develop 额外返回 Lightroom 显影参数表。',
          },
        },
        required: ['photo_id', 'style_id'],
      },
      output: output((_args, value) => {
        if (value.action === 'develop') {
          return text(
            [
              `已生成 Lightroom 显影参数与 XMP（${value.mapping_version}）。`,
              `风格：${value.style_name || value.style_id}，预设名：${value.preset_name}`,
              `关键参数：${parametersLine(value.parameters) || '—'}`,
              `XMP 文件：${value.xmp_path || value.xmp_url}`,
              `develop_settings 共 ${Object.keys(value.develop_settings || {}).length} 项。`,
            ].join('\n'),
          );
        }
        return text(
          [
            `已生成 XMP 预设。`,
            `预设名：${value.preset_name}`,
            `文件：${value.preset_path || value.download_url}`,
            `映射版本：${value.mapping_version}。`,
          ].join('\n'),
        );
      }),
      timeoutMs: 120_000,
      isConcurrencySafe: () => false,
      async execute(args) {
        const photoId = args && args.photo_id;
        const styleId = args && args.style_id;
        const intensity = args && args.intensity;
        if (args && args.action === 'develop') {
          const develop = await service.developSettings(photoId, styleId, intensity);
          return { ...develop, action: 'develop' };
        }
        const exported = await service.exportPreset(photoId, styleId, intensity);
        return { ...exported, action: 'xmp' };
      },
    }),
  );

  disposers.push(
    ctx.tools.register({
      name: 'photo_agent_lightroom',
      description:
        '查看或安装 Lightroom Classic 桥接插件，或把一套风格写入 Lightroom 预设目录并排队。send/install 会写用户的 Lightroom 目录，必须 confirm=true；send 只排队，用户在 Lightroom 中运行“应用 Web 端待处理修图”后才应用到照片。',
      parameters: {
        type: 'object',
        properties: {
          action: {
            type: 'string',
            enum: ['status', 'install', 'send'],
            description: 'status 只读状态；install 安装/检查桥接插件；send 写预设并排队。',
          },
          photo_id: { type: 'string', description: 'action=send 时必填，已分析照片的 ID。' },
          style_id: { type: 'string', description: 'action=send 时必填，已选风格 ID。' },
          intensity: { type: 'number', description: 'action=send 时的风格强度，0–1，默认 0.7（70%）。' },
          confirm: {
            type: 'boolean',
            description: 'install/send 必须显式设为 true；否则只返回将要发生什么的预览。',
          },
        },
        required: ['action'],
      },
      output: output((_args, value) => {
        if (value.requires_confirmation) {
          return text(`需要确认：${value.preview}\n确认后请带 confirm=true 再调用一次。`);
        }
        if (value.action === 'status') {
          const lr = value.lightroom || {};
          return text(
            [
              `Lightroom 检测：${lr.detected ? '已检测到' : '未检测到'}；插件：${lr.plugin_installed ? '已注册' : '未注册'}。`,
              `服务地址：${lr.server_url || '—'}`,
              `预设目录：${lr.presets_dir || '—'}`,
              `桥接收件箱：${lr.bridge_inbox || '—'}`,
              lr.message || '',
            ]
              .filter(Boolean)
              .join('\n'),
          );
        }
        if (value.action === 'install') {
          return text(value.install?.message || 'Lightroom 插件安装检查已完成。');
        }
        return text(
          [
            value.message || '已写入 Lightroom 预设并排队。',
            value.job_id ? `job_id=${value.job_id}` : '',
            value.preset_path ? `预设文件：${value.preset_path}` : '',
            value.bridge_inbox ? `收件箱：${value.bridge_inbox}` : '',
            '下一步：在 Lightroom Classic 中选中目标照片，运行“图库 > 增效工具附加功能 > AI Photo Agent - 应用 Web 端待处理修图”。',
          ]
            .filter(Boolean)
            .join('\n'),
        );
      }),
      timeoutMs: 120_000,
      isConcurrencySafe: () => false,
      async execute(args) {
        const action = (args && args.action) || 'status';
        if (action === 'status') {
          const lightroom = await service.lightroomStatus();
          return { action, lightroom };
        }
        if (action === 'install') {
          if (!readConfirm(args)) {
            return {
              requires_confirmation: true,
              preview:
                '将把 lightroom/AIPhotoAgent.lrplugin 复制到 Lightroom 的 Modules 目录。该操作会写用户目录，但不修改照片。',
            };
          }
          const install = await service.installLightroomPlugin();
          return { action, install };
        }
        if (action === 'send') {
          const photoId = args && args.photo_id;
          const styleId = args && args.style_id;
          if (!photoId || !styleId) {
            throw new Error('action=send 需要 photo_id 和 style_id。');
          }
          if (!readConfirm(args)) {
            return {
              requires_confirmation: true,
              preview: `将把照片 ${photoId} 的风格 ${styleId}（强度 ${args.intensity ?? 0.7}）写入 Lightroom 预设目录，并在桥接收件箱排队；不会立即修改照片。`,
            };
          }
          const result = await service.sendToLightroom({
            photoId,
            styleId,
            intensity: args.intensity,
            installPlugin: true,
          });
          return { action, ...result };
        }
        throw new Error('未知的 Lightroom 操作。');
      },
    }),
  );

  disposers.push(
    ctx.tools.register({
      name: 'photo_agent_learning',
      description:
        '摄影学习：创建学习主题、上传参考图（最多 8 张）、分析参考图，返回构图/主体/色彩/影调/练习要点与跨图总结。deepseek Provider 下学习模块仍使用后端 Mock 分析。',
      parameters: {
        type: 'object',
        properties: {
          action: {
            type: 'string',
            enum: ['create', 'add_reference', 'analyze', 'get'],
            description: 'create 创建主题；add_reference 上传参考图；analyze 分析；get 读取。',
          },
          session_id: { type: 'string', description: 'action=add_reference/analyze/get 时必填。' },
          topic: { type: 'string', description: 'action=create 时必填，学习主题，最多 120 字。' },
          path: { type: 'string', description: 'action=add_reference 时必填，参考图本地路径（JPG/PNG）。' },
          source_url: { type: 'string', description: '参考图来源 URL，可选。' },
          author: { type: 'string', description: '参考图作者/来源名称，可选。' },
          license: { type: 'string', description: '参考图许可说明，可选。' },
        },
        required: ['action'],
      },
      output: output((_args, value) => {
        const session = value.session || {};
        const summary = session.summary;
        const references = Array.isArray(session.references) ? session.references : [];
        if (value.action === 'add_reference') {
          return text(
            `已上传参考图。学习会话 ${session.id || ''} 当前有 ${references.length}/8 张参考图；可继续上传或调用 analyze。`,
          );
        }
        if (!summary) {
          return text(
            `学习会话 ${session.id || ''}：${session.topic || ''}，已有 ${references.length} 张参考图，尚未分析。`,
          );
        }
        const refLines = references
          .slice(0, 4)
          .map((reference, index) => {
            const lessons = reference.analysis?.practical_lessons || [];
            return `${index + 1}. ${lessons.slice(0, 2).join('；') || '（无要点）'}`;
          })
          .join('\n');
        return text(
          [
            `学习总结：${summary.style_overview}`,
            `常见构图：\n${bulletList(summary.common_composition_patterns, 3)}`,
            `色彩策略：\n${bulletList(summary.common_color_strategies, 3)}`,
            `练习：\n${bulletList(summary.exercises, 3)}`,
            `参考图要点：\n${refLines || '—'}`,
            `session_id=${session.id}。`,
          ].join('\n\n'),
        );
      }),
      timeoutMs: 180_000,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const action = (args && args.action) || 'create';
        if (action === 'create') {
          const session = await service.learningCreate(args && args.topic);
          return { action, session };
        }
        if (action === 'get') {
          if (!args || !args.session_id) throw new Error('action=get 需要 session_id。');
          const session = await service.learningGet(args.session_id);
          return { action, session };
        }
        if (action === 'analyze') {
          if (!args || !args.session_id) throw new Error('action=analyze 需要 session_id。');
          const session = await service.learningAnalyze(args.session_id);
          return { action, session };
        }
        if (action === 'add_reference') {
          if (!args || !args.session_id) throw new Error('action=add_reference 需要 session_id。');
          const filePath = await resolveImagePath(ctx, exec, args.path);
          const data = await readLocalImage(filePath, config.maxUploadBytes);
          const extension = path.extname(filePath).toLowerCase();
          if (extension !== '.jpg' && extension !== '.jpeg' && extension !== '.png') {
            throw new Error('学习参考图只支持 JPG/JPEG 和 PNG。');
          }
          await service.learningAddReference({
            sessionId: args.session_id,
            data,
            filename: path.basename(filePath),
            contentType: contentTypeFor(filePath),
            sourceUrl: args.source_url,
            author: args.author,
            license: args.license,
          });
          const session = await service.learningGet(args.session_id);
          return { action, session };
        }
        throw new Error('未知的学习操作。');
      },
    }),
  );

  return disposers;
}
