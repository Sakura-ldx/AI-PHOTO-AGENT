/**
 * Local JSON + binary routes used by the plugin's React panel.
 *
 * The routes are mounted on the harness web server under `/photo-agent/api`.
 * They never accept an arbitrary backend URL: every operation goes through the
 * shared PhotoService, so the panel and the agent tools use one code path.
 *
 * @module @local/dsh-photo-agent/routes
 */

import { PhotoError, errorMessage } from './errors.js';

export const ROUTE_PREFIX = '/photo-agent/api';

const MAX_JSON_BYTES = 256 * 1024;

function sendJson(res, value, status = 200) {
  let body;
  try {
    body = JSON.stringify(value);
  } catch (error) {
    res.statusCode = 500;
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.end(JSON.stringify({ ok: false, error: `响应序列化失败：${errorMessage(error)}` }));
    return;
  }
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.end(body);
}

function sendError(res, error) {
  const status = error instanceof PhotoError ? error.status : 500;
  const code = error instanceof PhotoError ? error.code : 'internal-error';
  const body = { ok: false, error: errorMessage(error), code };
  if (error instanceof PhotoError && error.detail) body.detail = error.detail;
  sendJson(res, body, status);
}

function requestAbortedError() {
  return new PhotoError('请求已中断，上传没有完成。', {
    status: 400,
    code: 'request-aborted',
  });
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const declared = Number(req.headers['content-length'] || 0);
    if (Number.isFinite(declared) && declared > limit) {
      try {
        req.resume?.();
      } catch {
        /* ignore */
      }
      reject(new PhotoError(`请求体超过 ${Math.round(limit / (1024 * 1024))} MB 上限。`, {
        status: 413,
        code: 'request-too-large',
      }));
      return;
    }
    const chunks = [];
    let size = 0;
    let settled = false;
    const cleanup = () => {
      req.off?.('data', onData);
      req.off?.('end', onEnd);
      req.off?.('error', onError);
      req.off?.('aborted', onAborted);
      req.off?.('close', onClose);
    };
    const settle = (fn, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      fn(value);
    };
    const tooLarge = () =>
      new PhotoError(`请求体超过 ${Math.round(limit / (1024 * 1024))} MB 上限。`, {
        status: 413,
        code: 'request-too-large',
      });
    const onData = (chunk) => {
      if (settled) return;
      size += chunk.length;
      if (size > limit) {
        chunks.length = 0;
        // Drain the rest instead of destroying the socket: a browser treats the
        // reset connection as "Failed to fetch" and never sees the 413 message.
        try {
          req.resume?.();
        } catch {
          /* ignore */
        }
        settle(reject, tooLarge());
        return;
      }
      chunks.push(chunk);
    };
    const onEnd = () => settle(resolve, Buffer.concat(chunks));
    const onError = (error) =>
      settle(
        reject,
        new PhotoError(`读取请求体失败：${errorMessage(error)}`, {
          status: 400,
          code: 'body-read-failed',
        }),
      );
    // A client that disconnects mid-upload must settle the promise instead of
    // leaving the route (and its buffers) pending forever.
    const onAborted = () => settle(reject, requestAbortedError());
    const onClose = () => {
      if (!req.complete) settle(reject, requestAbortedError());
    };
    req.on('data', onData);
    req.on('end', onEnd);
    req.on('error', onError);
    req.on?.('aborted', onAborted);
    req.on?.('close', onClose);
  });
}

async function readJson(req, limit = MAX_JSON_BYTES) {
  const body = await readBody(req, limit);
  if (body.length === 0) return {};
  try {
    const value = JSON.parse(body.toString('utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('not an object');
    }
    return value;
  } catch {
    throw new PhotoError('请求体必须是 JSON 对象。', { status: 400, code: 'bad-json' });
  }
}

function decodeHeader(value) {
  if (value === undefined || value === null) return '';
  let text = Array.isArray(value) ? value[0] : String(value);
  text = text.replace(/[\r\n]/g, '');
  try {
    text = decodeURIComponent(text);
  } catch {
    /* leave it as-is */
  }
  return text.trim();
}

const LOOPBACK_HOSTNAMES = new Set(['127.0.0.1', 'localhost', '::1']);

/**
 * Parse a `Host` header and accept only loopback hosts.
 *
 * Any port is accepted: the Harness web server may listen on 2739, while smoke
 * harnesses use their own ports; the host name is what matters for DNS
 * rebinding protection.
 *
 * @param {unknown} value
 * @returns {{ host: string } | null}
 */
function parseLoopbackHost(value) {
  if (typeof value !== 'string') return null;
  const header = value.trim();
  if (!header || header.length > 255 || /\s/.test(header)) return null;
  let parsed;
  try {
    parsed = new URL(`http://${header}`);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:') return null;
  if (parsed.username || parsed.password) return null;
  if (parsed.pathname !== '/' || parsed.search || parsed.hash) return null;
  const hostname = parsed.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (!LOOPBACK_HOSTNAMES.has(hostname)) return null;
  return { host: parsed.host.toLowerCase() };
}

/**
 * Reject requests whose Host is not loopback, or whose Origin disagrees with
 * Host. Requests without an Origin header (agent tools, curl, server-to-server)
 * stay allowed after the Host check; browsers always attach Origin for
 * cross-origin writes and attach Sec-Fetch-Site for all requests.
 *
 * @param {import('node:http').IncomingMessage} req
 * @returns {{ error: string, code: string } | null}
 */
function requestGuard(req) {
  const host = parseLoopbackHost(req.headers.host);
  if (!host) {
    return { error: '只接受指向本机回环地址的插件请求。', code: 'host-rejected' };
  }
  const origin = req.headers.origin;
  if (origin !== undefined) {
    let parsed;
    try {
      parsed = new URL(String(origin));
    } catch {
      return { error: '请求来源不合法。', code: 'origin-rejected' };
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return { error: '跨站请求已拒绝。', code: 'origin-rejected' };
    }
    if (parsed.host.toLowerCase() !== host.host) {
      return { error: '跨站请求已拒绝。', code: 'origin-rejected' };
    }
  }
  const site = req.headers['sec-fetch-site'];
  if (typeof site === 'string' && site !== 'same-origin' && site !== 'none') {
    return { error: '跨站请求已拒绝。', code: 'origin-rejected' };
  }
  return null;
}

/**
 * @param {{
 *   service: ReturnType<import('./service.js').createPhotoService>,
 *   config: ReturnType<import('./config.js').loadConfig>,
 *   logger?: { warn?: Function, error?: Function },
 * }} deps
 */
export function createRouteHandler({ service, config, logger }) {
  return async function handler(req, res) {
    const method = String(req.method || 'GET').toUpperCase();
    let url;
    try {
      url = new URL(req.url || '/', 'http://127.0.0.1');
    } catch {
      sendJson(res, { ok: false, error: '请求地址不合法。' }, 400);
      return;
    }
    const routePath = url.pathname.slice(ROUTE_PREFIX.length) || '/';

    try {
      // Enforce the loopback Host / same-origin rule for every method before
      // routing. Agent tool calls go through the service directly and never
      // need an Origin header, so missing Origin stays allowed.
      const guard = requestGuard(req);
      if (guard) {
        sendJson(res, { ok: false, error: guard.error, code: guard.code }, 403);
        return;
      }

      if (method === 'OPTIONS') {
        res.statusCode = 204;
        res.end();
        return;
      }

      if (method === 'GET' && routePath === '/state') {
        sendJson(res, { ok: true, ...service.describe() });
        return;
      }

      if (method === 'GET' && routePath === '/asset') {
        const assetPath = url.searchParams.get('path') || '';
        const asset = await service.fetchAsset(assetPath);
        res.statusCode = asset.status;
        res.setHeader('Content-Type', asset.content_type);
        res.setHeader('Content-Length', String(asset.buffer.length));
        res.setHeader('Cache-Control', 'no-store');
        if (asset.content_disposition) {
          res.setHeader('Content-Disposition', asset.content_disposition);
        }
        res.end(asset.buffer);
        return;
      }

      if (method === 'GET' && routePath === '/knowledge/overview') {
        sendJson(res, { ok: true, ...(await service.knowledgeOverview()) });
        return;
      }

      if (method === 'GET' && routePath === '/knowledge/summaries') {
        const list = await service.knowledgeList({
          dimension: url.searchParams.get('dimension') || undefined,
          tag: url.searchParams.get('tag') || undefined,
          category: url.searchParams.get('category') || undefined,
          limit: url.searchParams.get('limit') || undefined,
          offset: url.searchParams.get('offset') || undefined,
        });
        sendJson(res, { ok: true, ...list });
        return;
      }

      if (method === 'GET') {
        const knowledgeMatch = routePath.match(/^\/knowledge\/summaries\/([0-9a-f]{32})$/);
        if (knowledgeMatch) {
          const summary = await service.knowledgeGet(knowledgeMatch[1]);
          sendJson(res, { ok: true, summary });
          return;
        }
      }

      if (method === 'GET') {
        const learningMatch = routePath.match(/^\/learning\/([0-9a-f]{32})$/);
        if (learningMatch) {
          const session = await service.learningGet(learningMatch[1]);
          sendJson(res, { ok: true, session });
          return;
        }
      }

      if (method === 'POST') {
        if (routePath === '/ensure') {
          sendJson(res, { ok: true, ...(await service.ensureWithLightroom()) });
          return;
        }

        if (routePath === '/upload') {
          const data = await readBody(req, config.maxUploadBytes);
          const filename = decodeHeader(req.headers['x-photo-filename']) || 'upload.jpg';
          const contentType = String(req.headers['content-type'] || 'application/octet-stream')
            .split(';')[0]
            .trim();
          const photo = await service.uploadImage({ data, filename, contentType });
          sendJson(res, { ok: true, photo });
          return;
        }

        if (routePath === '/analyze') {
          const body = await readJson(req);
          const analysis = await service.analyze(body.photo_id, body.provider || 'default');
          sendJson(res, { ok: true, analysis });
          return;
        }

        if (routePath === '/preview') {
          const body = await readJson(req);
          const preview = await service.preview(body.photo_id, body.style_id, body.intensity);
          sendJson(res, { ok: true, preview });
          return;
        }

        if (routePath === '/export') {
          const body = await readJson(req);
          if (body.action === 'develop') {
            const develop = await service.developSettings(
              body.photo_id,
              body.style_id,
              body.intensity,
            );
            sendJson(res, { ok: true, develop });
          } else {
            const exported = await service.exportPreset(
              body.photo_id,
              body.style_id,
              body.intensity,
            );
            sendJson(res, { ok: true, export: exported });
          }
          return;
        }

        if (routePath === '/lightroom') {
          const body = await readJson(req);
          if (body.action === 'status') {
            const lightroom = await service.lightroomStatus();
            sendJson(res, { ok: true, lightroom });
            return;
          }
          if (body.action === 'install') {
            const install = await service.installLightroomPlugin();
            sendJson(res, { ok: true, install });
            return;
          }
          if (body.action === 'send') {
            const result = await service.sendToLightroom({
              photoId: body.photo_id,
              styleId: body.style_id,
              intensity: body.intensity,
              installPlugin: body.install_plugin !== false,
            });
            sendJson(res, { ok: true, result });
            return;
          }
          throw new PhotoError('未知的 Lightroom 操作。', { status: 400, code: 'bad-action' });
        }

        if (routePath === '/learning') {
          const body = await readJson(req);
          const session = await service.learningCreate(body.topic);
          sendJson(res, { ok: true, session });
          return;
        }

        const referenceMatch = routePath.match(/^\/learning\/([0-9a-f]{32})\/references$/);
        if (referenceMatch) {
          const data = await readBody(req, config.maxUploadBytes);
          const filename = decodeHeader(req.headers['x-photo-filename']) || 'reference.jpg';
          const contentType = String(req.headers['content-type'] || 'application/octet-stream')
            .split(';')[0]
            .trim();
          const reference = await service.learningAddReference({
            sessionId: referenceMatch[1],
            data,
            filename,
            contentType,
            sourceUrl: decodeHeader(req.headers['x-photo-source-url']) || undefined,
            author: decodeHeader(req.headers['x-photo-author']) || undefined,
            license: decodeHeader(req.headers['x-photo-license']) || undefined,
          });
          sendJson(res, { ok: true, reference });
          return;
        }

        const analyzeMatch = routePath.match(/^\/learning\/([0-9a-f]{32})\/analyze$/);
        if (analyzeMatch) {
          const session = await service.learningAnalyze(analyzeMatch[1]);
          sendJson(res, { ok: true, session });
          return;
        }

        const rebuildMatch = routePath.match(/^\/knowledge\/summaries\/([0-9a-f]{32})\/rebuild$/);
        if (rebuildMatch) {
          const summary = await service.knowledgeRebuild(rebuildMatch[1]);
          sendJson(res, { ok: true, summary });
          return;
        }

        if (routePath === '/state/reset') {
          service.store.reset();
          sendJson(res, { ok: true });
          return;
        }
      }

      sendJson(res, { ok: false, error: '未知接口。', code: 'not-found' }, 404);
    } catch (error) {
      if (!(error instanceof PhotoError)) {
        logger?.error?.(`[photo-agent] 路由失败：${errorMessage(error)}`);
      }
      sendError(res, error);
    }
  };
}
