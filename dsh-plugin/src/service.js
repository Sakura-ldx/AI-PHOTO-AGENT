/**
 * One operation layer shared by the panel's HTTP routes and the agent tools.
 *
 * Every method talks to the existing FastAPI backend over loopback HTTP. The
 * plugin deliberately owns no image processing, analysis prompt, XMP mapping or
 * Lightroom queue format of its own.
 *
 * @module @local/dsh-photo-agent/service
 */

import path from 'node:path';
import { assetAbsolutePath, isAllowedAssetPath, normalizeAssetPath } from './assets.js';
import { publicConfig } from './config.js';
import {
  PhotoError,
  errorMessage,
  isConnectionRefused,
  networkError,
  upstreamError,
} from './errors.js';

const PROVIDERS = new Set(['default', 'deepseek', 'mock', 'openai', 'codex']);
const EXTENSION_TYPES = new Map([
  ['.jpg', 'image/jpeg'],
  ['.jpeg', 'image/jpeg'],
  ['.png', 'image/png'],
  ['.tif', 'image/tiff'],
  ['.tiff', 'image/tiff'],
  ['.cr3', 'image/x-canon-cr3'],
]);
const MAX_ASSET_BYTES = 64 * 1024 * 1024;
const JSON_TIMEOUT_MS = 30_000;
const UPLOAD_TIMEOUT_MS = 120_000;

export function contentTypeFor(filename) {
  const extension = path.extname(String(filename || '')).toLowerCase();
  return EXTENSION_TYPES.get(extension) || 'application/octet-stream';
}

export function safeFilename(filename, fallback = 'upload.jpg') {
  let value = String(filename || '')
    .replace(/\\/g, '/')
    .split('/')
    .pop() || '';
  value = value.replace(/[\u0000-\u001f\u007f"<>|:*?]/g, '_').trim();
  if (!value || value === '.' || value === '..') return fallback;
  return value.slice(0, 160);
}

function timeoutSignal(ms) {
  if (typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(ms);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  if (typeof timer.unref === 'function') timer.unref();
  return controller.signal;
}

function combineSignal(signal, ms) {
  const timeout = timeoutSignal(ms);
  if (!signal) return timeout;
  if (typeof AbortSignal.any === 'function') return AbortSignal.any([signal, timeout]);
  return signal;
}

function assertPhotoId(value) {
  const id = String(value || '');
  if (!/^[0-9a-f]{32}$/.test(id)) {
    throw new PhotoError('photo_id 必须是 32 位小写十六进制字符串。', {
      status: 400,
      code: 'invalid-photo-id',
    });
  }
  return id;
}

function assertSessionId(value) {
  const id = String(value || '');
  if (!/^[0-9a-f]{32}$/.test(id)) {
    throw new PhotoError('session_id 必须是 32 位小写十六进制字符串。', {
      status: 400,
      code: 'invalid-session-id',
    });
  }
  return id;
}

function assertStyleId(value) {
  const id = String(value || '');
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id)) {
    throw new PhotoError('style_id 格式不合法。', { status: 400, code: 'invalid-style-id' });
  }
  return id;
}

function assertIntensity(value) {
  const number = value === undefined || value === null ? 0.7 : Number(value);
  if (!Number.isFinite(number) || number < 0 || number > 1) {
    throw new PhotoError('intensity 必须是 0 到 1 之间的数字。', {
      status: 400,
      code: 'invalid-intensity',
    });
  }
  return number;
}

function assertProvider(value) {
  const provider = String(value || 'default').toLowerCase();
  if (!PROVIDERS.has(provider)) {
    throw new PhotoError('provider 必须是 default、deepseek、mock、openai 或 codex。', {
      status: 400,
      code: 'invalid-provider',
    });
  }
  return provider;
}

function parseJsonText(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/**
 * @param {{
 *   config: ReturnType<import('./config.js').loadConfig>,
 *   backend: ReturnType<import('./backend.js').createBackend>,
 *   store: ReturnType<import('./store.js').createStateStore>,
 *   logger?: { info?: Function, warn?: Function, error?: Function },
 *   fetch?: typeof fetch,
 * }} deps
 */
export function createPhotoService({ config, backend, store, logger, fetch: fetchImpl = globalThis.fetch }) {
  async function ensureReady() {
    return backend.ensureReady();
  }

  async function parseJsonResponse(response) {
    const text = await response.text();
    const payload = parseJsonText(text);
    if (!response.ok) {
      throw upstreamError(response.status, payload, `后端返回 HTTP ${response.status}。`);
    }
    if (payload === null) {
      throw new PhotoError('后端返回了非 JSON 响应。', {
        status: 502,
        code: 'backend-invalid-response',
      });
    }
    return payload;
  }

  async function requestBackend(url, init, timeoutMs, action = '连接后端', callerSignal) {
    const send = () =>
      fetchImpl(url, {
        ...init,
        signal: combineSignal(callerSignal, timeoutMs),
      });
    let response;
    try {
      response = await send();
    } catch (error) {
      const state = backend.status?.() || {};
      // Only retry when the request never reached a *managed* backend: the
      // connection was refused, so replaying it cannot duplicate side effects.
      // Timeouts and resets are not replayed because the backend may have
      // already processed the request.
      const recoverable = Boolean(state.ready && state.managed) && isConnectionRefused(error);
      if (!recoverable) throw networkError(error, config.baseUrl, action);
      logger?.warn?.(
        `[photo-agent] 托管后端连接中断，重新探测后重试一次：${errorMessage(error)}`,
      );
      backend.markUnready?.();
      await ensureReady();
      try {
        response = await send();
      } catch (retryError) {
        throw networkError(retryError, config.baseUrl, action);
      }
    }
    return parseJsonResponse(response);
  }

  async function httpJson(method, apiPath, body, options = {}) {
    const timeoutMs = options.timeoutMs || config.analysisTimeoutMs;
    const headers = { accept: 'application/json' };
    let payloadBody;
    if (body !== undefined) {
      headers['content-type'] = 'application/json';
      payloadBody = JSON.stringify(body);
    }
    return requestBackend(
      `${config.baseUrl}${apiPath}`,
      { method, headers, body: payloadBody },
      timeoutMs,
      '连接后端',
      options.signal,
    );
  }

  async function httpMultipart(apiPath, form, options = {}) {
    return requestBackend(
      `${config.baseUrl}${apiPath}`,
      { method: 'POST', body: form },
      options.timeoutMs || UPLOAD_TIMEOUT_MS,
      '连接后端',
      options.signal,
    );
  }

  async function ensureWithLightroom() {
    await ensureReady();
    let lightroom = null;
    let lightroomError = null;
    try {
      lightroom = await httpJson('GET', '/api/lightroom/status', undefined, {
        timeoutMs: JSON_TIMEOUT_MS,
      });
    } catch (error) {
      lightroomError = errorMessage(error);
      logger?.warn?.(`[photo-agent] 读取 Lightroom 状态失败：${lightroomError}`);
    }
    return {
      config: publicConfig(config),
      backend: backend.status(),
      lightroom,
      lightroom_error: lightroomError,
    };
  }

  function describe() {
    return {
      config: publicConfig(config),
      backend: backend.status(),
      last: store.read(),
    };
  }

  async function uploadImage({ data, filename, contentType }) {
    if (!Buffer.isBuffer(data) || data.length === 0) {
      throw new PhotoError('没有收到图片内容。', { status: 400, code: 'empty-upload' });
    }
    if (data.length > config.maxUploadBytes) {
      throw new PhotoError(
        `图片超过 ${Math.round(config.maxUploadBytes / (1024 * 1024))} MB 上限。`,
        { status: 413, code: 'upload-too-large' },
      );
    }
    const safe = safeFilename(filename);
    const type = contentType && contentType !== 'application/octet-stream'
      ? contentType
      : contentTypeFor(safe);
    if (!EXTENSION_TYPES.has(path.extname(safe).toLowerCase())) {
      throw new PhotoError('只支持 JPG/JPEG、PNG、TIF/TIFF 和 Canon CR3 文件。', {
        status: 400,
        code: 'unsupported-image',
      });
    }
    await ensureReady();
    const form = new FormData();
    form.append('file', new Blob([data], { type }), safe);
    const payload = await httpMultipart('/api/photos', form);
    store.savePhoto({
      photo_id: payload.photo_id,
      metadata: payload.metadata,
      analysis: null,
      style_id: null,
      intensity: 0.7,
    });
    return payload;
  }

  async function analyze(photoId, providerName = 'default') {
    const id = assertPhotoId(photoId);
    const provider = assertProvider(providerName);
    await ensureReady();
    const payload = await httpJson(
      'POST',
      `/api/photos/${id}/analyze`,
      { provider },
      { timeoutMs: config.analysisTimeoutMs },
    );
    const previous = store.read();
    const samePhoto = previous && previous.photo_id === id;
    store.savePhoto({
      photo_id: id,
      metadata: samePhoto ? previous.metadata : null,
      analysis: payload,
      style_id: payload.recommended_styles?.[0]?.id || null,
      intensity: 0.7,
    });
    return payload;
  }

  async function preview(photoId, styleId, intensity) {
    const id = assertPhotoId(photoId);
    const style = assertStyleId(styleId);
    const value = assertIntensity(intensity);
    await ensureReady();
    const payload = await httpJson('POST', `/api/photos/${id}/preview`, {
      style_id: style,
      intensity: value,
    });
    store.saveSelection({ photo_id: id, style_id: style, intensity: value });
    return {
      ...payload,
      preview_path: assetAbsolutePath(config.dataRoot, payload.preview_url),
    };
  }

  async function exportPreset(photoId, styleId, intensity) {
    const id = assertPhotoId(photoId);
    const style = assertStyleId(styleId);
    const value = assertIntensity(intensity);
    await ensureReady();
    const payload = await httpJson('POST', `/api/photos/${id}/export`, {
      style_id: style,
      intensity: value,
    });
    store.saveSelection({ photo_id: id, style_id: style, intensity: value });
    return {
      ...payload,
      preset_path: assetAbsolutePath(config.dataRoot, payload.download_url),
    };
  }

  async function developSettings(photoId, styleId, intensity) {
    const id = assertPhotoId(photoId);
    const style = assertStyleId(styleId);
    const value = assertIntensity(intensity);
    await ensureReady();
    const payload = await httpJson('POST', `/api/photos/${id}/develop`, {
      style_id: style,
      intensity: value,
    });
    store.saveSelection({ photo_id: id, style_id: style, intensity: value });
    return {
      ...payload,
      xmp_path: assetAbsolutePath(config.dataRoot, payload.xmp_url),
    };
  }

  async function lightroomStatus() {
    await ensureReady();
    return httpJson('GET', '/api/lightroom/status', undefined, { timeoutMs: JSON_TIMEOUT_MS });
  }

  async function installLightroomPlugin() {
    await ensureReady();
    return httpJson('POST', '/api/lightroom/install-plugin', undefined, {
      timeoutMs: JSON_TIMEOUT_MS,
    });
  }

  async function sendToLightroom({ photoId, styleId, intensity, installPlugin = true }) {
    const id = assertPhotoId(photoId);
    const style = assertStyleId(styleId);
    const value = assertIntensity(intensity);
    await ensureReady();
    const payload = await httpJson('POST', `/api/photos/${id}/lightroom`, {
      style_id: style,
      intensity: value,
      install_plugin: Boolean(installPlugin),
    });
    store.saveSelection({ photo_id: id, style_id: style, intensity: value });
    return payload;
  }

  async function learningCreate(topic) {
    const value = String(topic || '').trim();
    if (!value) {
      throw new PhotoError('学习主题不能为空。', { status: 400, code: 'empty-topic' });
    }
    await ensureReady();
    const payload = await httpJson('POST', '/api/learning/sessions', { topic: value });
    store.saveLearning(payload.id);
    return payload;
  }

  async function learningGet(sessionId) {
    const id = assertSessionId(sessionId);
    await ensureReady();
    return httpJson('GET', `/api/learning/sessions/${id}`, undefined, {
      timeoutMs: JSON_TIMEOUT_MS,
    });
  }

  async function learningAddReference({
    sessionId,
    data,
    filename,
    contentType,
    sourceUrl,
    author,
    license,
  }) {
    const id = assertSessionId(sessionId);
    if (!Buffer.isBuffer(data) || data.length === 0) {
      throw new PhotoError('没有收到参考图内容。', { status: 400, code: 'empty-upload' });
    }
    if (data.length > config.maxUploadBytes) {
      throw new PhotoError(
        `参考图超过 ${Math.round(config.maxUploadBytes / (1024 * 1024))} MB 上限。`,
        { status: 413, code: 'upload-too-large' },
      );
    }
    const safe = safeFilename(filename, 'reference.jpg');
    const type = contentType && contentType !== 'application/octet-stream'
      ? contentType
      : contentTypeFor(safe);
    await ensureReady();
    const form = new FormData();
    form.append('file', new Blob([data], { type }), safe);
    if (sourceUrl) form.append('source_url', String(sourceUrl));
    if (author) form.append('author', String(author));
    if (license) form.append('license', String(license));
    const payload = await httpMultipart(`/api/learning/sessions/${id}/references`, form);
    store.saveLearning(id);
    return payload;
  }

  async function learningAnalyze(sessionId) {
    const id = assertSessionId(sessionId);
    await ensureReady();
    const payload = await httpJson('POST', `/api/learning/sessions/${id}/analyze`, undefined, {
      timeoutMs: config.analysisTimeoutMs,
    });
    store.saveLearning(id);
    return payload;
  }

  async function knowledgeOverview() {
    await ensureReady();
    return httpJson('GET', '/api/knowledge/overview', undefined, {
      timeoutMs: JSON_TIMEOUT_MS,
    });
  }

  function knowledgeQuery(params = {}) {
    const query = new URLSearchParams();
    if (params.dimension) query.set('dimension', String(params.dimension));
    if (params.tag) query.set('tag', String(params.tag));
    if (params.category) query.set('category', String(params.category));
    const limit = Number(params.limit);
    const offset = Number(params.offset);
    query.set('limit', String(Number.isFinite(limit) && limit > 0 ? Math.min(200, Math.floor(limit)) : 50));
    query.set('offset', String(Number.isFinite(offset) && offset > 0 ? Math.floor(offset) : 0));
    return query.toString();
  }

  async function knowledgeList(params = {}) {
    await ensureReady();
    return httpJson('GET', `/api/knowledge/summaries?${knowledgeQuery(params)}`, undefined, {
      timeoutMs: JSON_TIMEOUT_MS,
    });
  }

  async function knowledgeGet(photoId) {
    const id = assertPhotoId(photoId);
    await ensureReady();
    return httpJson('GET', `/api/knowledge/summaries/${id}`, undefined, {
      timeoutMs: JSON_TIMEOUT_MS,
    });
  }

  async function knowledgeRebuild(photoId) {
    const id = assertPhotoId(photoId);
    await ensureReady();
    return httpJson('POST', `/api/knowledge/summaries/${id}/rebuild`, undefined, {
      timeoutMs: config.analysisTimeoutMs,
    });
  }

  async function fetchAsset(assetPath, options = {}) {
    // Allowlist the canonical pathname and fetch exactly that value: the raw
    // input may still contain encoded dot segments that a later URL parser
    // would normalise into a different backend path.
    const normalized = normalizeAssetPath(assetPath);
    if (!normalized || !isAllowedAssetPath(normalized)) {
      throw new PhotoError('不允许访问该资源路径。', { status: 403, code: 'asset-rejected' });
    }
    await ensureReady();
    let response;
    try {
      response = await fetchImpl(`${config.baseUrl}${normalized}`, {
        method: 'GET',
        headers: { accept: '*/*' },
        signal: combineSignal(options.signal, JSON_TIMEOUT_MS),
      });
    } catch (error) {
      throw networkError(error, config.baseUrl, '读取资源');
    }
    if (!response.ok) {
      let payload = null;
      try {
        payload = parseJsonText(await response.text());
      } catch {
        /* binary error body */
      }
      throw upstreamError(response.status, payload, `后端返回 HTTP ${response.status}。`);
    }
    const declared = Number(response.headers.get('content-length') || 0);
    if (declared > MAX_ASSET_BYTES) {
      throw new PhotoError('资源文件过大，无法通过面板代理。', {
        status: 413,
        code: 'asset-too-large',
      });
    }
    const buffer = Buffer.from(await response.arrayBuffer());
    if (buffer.length > MAX_ASSET_BYTES) {
      throw new PhotoError('资源文件过大，无法通过面板代理。', {
        status: 413,
        code: 'asset-too-large',
      });
    }
    return {
      status: response.status,
      content_type: response.headers.get('content-type') || 'application/octet-stream',
      content_disposition: response.headers.get('content-disposition') || null,
      buffer,
    };
  }

  return {
    ensureReady,
    ensureWithLightroom,
    describe,
    uploadImage,
    analyze,
    preview,
    exportPreset,
    developSettings,
    lightroomStatus,
    installLightroomPlugin,
    sendToLightroom,
    learningCreate,
    learningGet,
    learningAddReference,
    learningAnalyze,
    knowledgeOverview,
    knowledgeList,
    knowledgeGet,
    knowledgeRebuild,
    fetchAsset,
    dispose: () => backend.dispose(),
    store,
    config,
  };
}
