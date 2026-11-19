/**
 * Host-side checks that need no running backend or browser.
 *
 * Covers configuration parsing, asset allowlisting, error mapping, the state
 * cache, and the backend lifecycle with injected fake spawn/fetch functions.
 *
 * Run: node scripts/check-host.mjs
 */

import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isAllowedAssetPath, normalizeAssetPath, assetAbsolutePath } from '../src/assets.js';
import { createBackend } from '../src/backend.js';
import { loadConfig } from '../src/config.js';
import { PhotoError, networkError, upstreamError } from '../src/errors.js';
import { createRouteHandler, ROUTE_PREFIX } from '../src/routes.js';
import { createPhotoService, contentTypeFor, safeFilename } from '../src/service.js';
import { createStateStore } from '../src/store.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const failures = [];
function check(condition, message) {
  if (!condition) failures.push(message);
}

function assertThrowsCode(fn, code) {
  return fn().then(
    () => {
      failures.push(`expected rejection with ${code}`);
    },
    (error) => check(error instanceof PhotoError && error.code === code, `expected ${code}, got ${error.code || error.message}`),
  );
}

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.killed = false;
  child.kill = (signal) => {
    child.killed = true;
    if (child.exitCode === null && child.signalCode === null) {
      child.exitCode = 0;
      // Real ChildProcess emits 'exit' asynchronously; the fake does it
      // synchronously so stopChild() exercises the wait-for-exit path.
      child.emit('exit', 0, signal || null);
    }
    return true;
  };
  return child;
}

function fakeRequest({ method = 'GET', url = '/', headers = {} } = {}) {
  const req = new EventEmitter();
  req.method = method;
  req.url = url;
  req.headers = { ...headers };
  if (!Object.prototype.hasOwnProperty.call(req.headers, 'host')) {
    req.headers.host = '127.0.0.1:8011';
  }
  req.complete = true;
  req.resume = () => {};
  return req;
}

function fakeResponse() {
  const res = new EventEmitter();
  res.statusCode = 200;
  res.headers = {};
  res.chunks = [];
  res.setHeader = (name, value) => {
    res.headers[String(name).toLowerCase()] = value;
  };
  res.end = (chunk) => {
    if (chunk !== undefined && chunk !== null) {
      res.chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk)));
    }
    res.ended = true;
  };
  return res;
}

function parseResponseBody(res) {
  const text = res.chunks.length ? Buffer.concat(res.chunks).toString('utf8') : '';
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function createRouteHarness(config) {
  const calls = [];
  const service = {
    describe: () => ({ config: {}, backend: { ready: true, managed: false }, last: null }),
    ensureWithLightroom: async () => ({
      config: {},
      backend: {},
      lightroom: null,
      lightroom_error: null,
    }),
    fetchAsset: async (assetPath) => {
      calls.push(assetPath);
      return {
        status: 200,
        content_type: 'image/jpeg',
        content_disposition: null,
        buffer: Buffer.from([0xff, 0xd8, 0xff]),
      };
    },
    uploadImage: async ({ data, filename }) => ({
      photo_id: 'a'.repeat(32),
      metadata: { width: 1, height: 1 },
      display_url: `/api/photos/${'a'.repeat(32)}/display`,
      bytes: data.length,
      filename,
    }),
    analyze: async () => ({ recommended_styles: [] }),
    learningGet: async () => ({ id: 'b'.repeat(32), references: [] }),
    knowledgeOverview: async () => ({
      total_summaries: 1,
      covered_categories: 1,
      average_intensity: 0.7,
      latest_generated_at: null,
      facets: [],
      primary_categories: [{ category: '风光', count: 1 }],
    }),
    knowledgeList: async (params) => ({
      total: 1,
      limit: Number(params && params.limit) || 50,
      offset: Number(params && params.offset) || 0,
      items: [{ photo_id: 'a'.repeat(32) }],
      facets: [],
      primary_categories: [],
    }),
    knowledgeGet: async (photoId) => ({ id: photoId, photo_id: photoId, directions: [] }),
    knowledgeRebuild: async (photoId) => ({ id: photoId, photo_id: photoId, directions: [] }),
    store: { reset: () => calls.push('reset') },
  };
  const handler = createRouteHandler({ service, config });
  const call = async (options = {}) => {
    const req = fakeRequest(options);
    const res = fakeResponse();
    await handler(req, res);
    return { req, res, body: parseResponseBody(res), calls };
  };
  return { calls, service, handler, call };
}

async function main() {
  // ---------------------------------------------------------------- config
  const defaults = loadConfig({});
  check(defaults.port === 8000, 'default port must be 8000');
  check(defaults.provider === 'deepseek', 'default provider must be deepseek');
  check(defaults.autoStart === true, 'default autoStart must be true');
  check(defaults.appDir === path.resolve(root, '..'), 'default appDir must be the repo root');
  check(defaults.stateFile.endsWith(path.join('data', 'runtime', 'dsh-photo-agent', 'state.json')), 'state file path mismatch');

  const overridden = loadConfig({
    DSH_PHOTO_AGENT_PORT: '8137',
    DSH_PHOTO_AGENT_PROVIDER: 'mock',
    DSH_PHOTO_AGENT_BASE_URL: 'http://127.0.0.1:9000',
    DSH_PHOTO_AGENT_AUTOSTART: 'false',
    DSH_PHOTO_AGENT_MAX_UPLOAD_BYTES: '1048576',
  });
  check(overridden.port === 8137, 'port override failed');
  check(overridden.provider === 'mock', 'provider override failed');
  check(overridden.baseUrl === 'http://127.0.0.1:9000', 'base URL override failed');
  check(overridden.externalBaseUrl === 'http://127.0.0.1:9000', 'external flag failed');
  check(overridden.autoStart === false, 'autoStart override failed');
  check(overridden.maxUploadBytes === 1048576, 'upload size override failed');
  check(overridden.childEnv.AI_PHOTO_AGENT_ANALYSIS_PROVIDER === 'mock', 'child env provider failed');

  let badUrl = false;
  try {
    loadConfig({ DSH_PHOTO_AGENT_BASE_URL: 'http://example.com:8000' });
  } catch {
    badUrl = true;
  }
  check(badUrl, 'non-loopback base URL must be rejected');

  // ---------------------------------------------------------------- assets
  check(normalizeAssetPath('/generated/x/y.jpg') === '/generated/x/y.jpg', 'valid generated path failed');
  check(normalizeAssetPath('/exports/a/b.xmp') === '/exports/a/b.xmp', 'valid exports path failed');
  check(isAllowedAssetPath('/api/photos/' + 'a'.repeat(32) + '/display'), 'display path must be allowed');
  check(
    isAllowedAssetPath('/api/learning/sessions/' + 'a'.repeat(32) + '/references/' + 'b'.repeat(32) + '/image'),
    'learning image path must be allowed',
  );
  check(!isAllowedAssetPath('/api/photos/../../etc/passwd'), 'traversal must be rejected');
  check(!isAllowedAssetPath('/generated/%2e%2e/%2e%2e/windows/win.ini'), 'encoded traversal must be rejected');
  check(!isAllowedAssetPath('http://evil.test/generated/x.jpg'), 'absolute URL must be rejected');
  check(!isAllowedAssetPath('//evil.test/generated/x.jpg'), 'protocol-relative URL must be rejected');
  check(!isAllowedAssetPath('/etc/passwd'), 'arbitrary path must be rejected');
  check(!isAllowedAssetPath('/generated/..\\x.jpg'), 'backslash must be rejected');
  check(!isAllowedAssetPath('/api/photos/nothex/display'), 'invalid photo id must be rejected');

  // Regression: multi-encoded traversal used to survive the 2-round decode and
  // be re-normalised by `new URL()` after the allowlist check.
  check(
    !isAllowedAssetPath('/generated/%25252e%25252e/%25252e%25252e/secret.txt'),
    'triple-encoded traversal must be rejected',
  );
  check(!isAllowedAssetPath('/generated/%2E%2E/secret.jpg'), 'mixed-case encoded traversal must be rejected');
  check(!isAllowedAssetPath('/generated/..%5c..%5cwin.ini'), 'encoded backslash traversal must be rejected');
  check(!isAllowedAssetPath('/generated/%zz/x.jpg'), 'invalid percent escape must be rejected');
  check(!isAllowedAssetPath('/generated/./x.jpg'), 'dot segment must be rejected');
  check(!isAllowedAssetPath('/generated/' + 'a'.repeat(3000)), 'overlong path must be rejected');
  check(!isAllowedAssetPath('/%2f%2fevil.test/generated/x.jpg'), 'encoded protocol-relative URL must be rejected');
  check(!isAllowedAssetPath('/generated/%252e%252e%252fetc%252fpasswd'), 'double-encoded slash traversal must be rejected');
  check(
    !isAllowedAssetPath('/generated/%25252e%25252e/api/photos/' + 'a'.repeat(32) + '/display'),
    'multi-encoded traversal into a display URL must be rejected',
  );
  check(
    normalizeAssetPath('/api/photos/' + 'a'.repeat(32) + '/display') === '/api/photos/' + 'a'.repeat(32) + '/display',
    'valid display path must normalise unchanged',
  );
  check(normalizeAssetPath('/generated/%2e%2e/x.jpg') === null, 'encoded traversal must normalize to null');

  const mapped = assetAbsolutePath('C:\\data', '/generated/abc/p.jpg');
  check(Boolean(mapped && mapped.endsWith(path.join('generated', 'previews', 'abc', 'p.jpg'))), 'generated path mapping failed');
  check(assetAbsolutePath('C:\\data', '/api/photos/' + 'a'.repeat(32) + '/display') === null, 'display has no mapped path');

  // ---------------------------------------------------------------- errors
  const timeout = networkError(Object.assign(new Error('aborted'), { name: 'TimeoutError' }), 'http://127.0.0.1:8000');
  check(timeout.status === 504 && timeout.code === 'backend-timeout', 'timeout mapping failed');
  const refused = networkError(Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } }), 'http://127.0.0.1:8000');
  check(refused.status === 503 && refused.code === 'backend-unreachable', 'refused mapping failed');
  const upstream = upstreamError(409, { detail: '最多 8 张' }, 'fallback');
  check(upstream.status === 409 && upstream.message.includes('8'), 'upstream mapping failed');

  // ---------------------------------------------------------------- routes
  // PA-06: Host/Origin guard must cover every method, and requests without an
  // Origin header (agent tools, curl) must still pass the Host check.
  const sameOrigin = { host: '127.0.0.1:8011', origin: 'http://127.0.0.1:8011' };
  {
    const routes = createRouteHarness(defaults);
    let result = await routes.call({ url: `${ROUTE_PREFIX}/state`, headers: sameOrigin });
    check(result.res.statusCode === 200 && result.body?.ok === true, 'same-origin GET /state must be allowed');

    result = await routes.call({ method: 'POST', url: `${ROUTE_PREFIX}/state/reset`, headers: sameOrigin });
    check(result.res.statusCode === 200 && result.body?.ok === true, 'same-origin POST must be allowed');

    result = await routes.call({
      url: `${ROUTE_PREFIX}/state`,
      headers: { host: '127.0.0.1:8011', origin: 'http://evil.test' },
    });
    check(
      result.res.statusCode === 403 && result.body?.code === 'origin-rejected',
      'cross-origin GET must be rejected',
    );

    result = await routes.call({
      method: 'POST',
      url: `${ROUTE_PREFIX}/state/reset`,
      headers: { host: '127.0.0.1:8011', origin: 'http://evil.test' },
    });
    check(
      result.res.statusCode === 403 && result.body?.code === 'origin-rejected',
      'cross-origin POST must be rejected',
    );

    result = await routes.call({ url: `${ROUTE_PREFIX}/state`, headers: { host: 'evil.test:8011' } });
    check(
      result.res.statusCode === 403 && result.body?.code === 'host-rejected',
      'non-loopback Host must be rejected',
    );

    result = await routes.call({ url: `${ROUTE_PREFIX}/state`, headers: { host: '127.0.0.1.evil.test:8011' } });
    check(
      result.res.statusCode === 403 && result.body?.code === 'host-rejected',
      'suffix-spoofed Host must be rejected',
    );

    result = await routes.call({ url: `${ROUTE_PREFIX}/state`, headers: { host: undefined } });
    check(result.res.statusCode === 403, 'missing Host must be rejected');

    result = await routes.call({ url: `${ROUTE_PREFIX}/state`, headers: { host: 'localhost:2739' } });
    check(result.res.statusCode === 200, 'loopback Host without Origin must be allowed');

    result = await routes.call({
      url: `${ROUTE_PREFIX}/state`,
      headers: { host: '[::1]:2739', origin: 'http://[::1]:2739' },
    });
    check(result.res.statusCode === 200, 'IPv6 loopback origin must be allowed');

    result = await routes.call({
      url: `${ROUTE_PREFIX}/state`,
      headers: { host: '127.0.0.1:8011', 'sec-fetch-site': 'cross-site' },
    });
    check(result.res.statusCode === 403, 'cross-site Sec-Fetch-Site must be rejected');

    result = await routes.call({
      url: `${ROUTE_PREFIX}/asset?path=${encodeURIComponent('/generated/previews/a.jpg')}`,
      headers: sameOrigin,
    });
    check(
      result.res.statusCode === 200 && result.calls.includes('/generated/previews/a.jpg'),
      'asset route must proxy the canonical allowed path',
    );

    result = await routes.call({ url: `${ROUTE_PREFIX}/knowledge/overview`, headers: sameOrigin });
    check(
      result.res.statusCode === 200 && result.body?.ok === true && result.body.total_summaries === 1,
      'knowledge overview route must proxy the backend response',
    );

    result = await routes.call({
      url: `${ROUTE_PREFIX}/knowledge/summaries?limit=10&tag=${encodeURIComponent('逆光')}&dimension=light`,
      headers: sameOrigin,
    });
    check(
      result.res.statusCode === 200 && result.body?.ok === true && result.body.total === 1,
      'knowledge list route must proxy the backend response',
    );

    const listedPhotoId = 'a'.repeat(32);
    result = await routes.call({
      url: `${ROUTE_PREFIX}/knowledge/summaries/${listedPhotoId}`,
      headers: sameOrigin,
    });
    check(
      result.res.statusCode === 200 && result.body?.ok === true && result.body.summary?.photo_id === listedPhotoId,
      'knowledge detail route must proxy the backend response',
    );

    result = await routes.call({
      method: 'POST',
      url: `${ROUTE_PREFIX}/knowledge/summaries/${listedPhotoId}/rebuild`,
      headers: sameOrigin,
    });
    check(
      result.res.statusCode === 200 && result.body?.ok === true && result.body.summary?.photo_id === listedPhotoId,
      'knowledge rebuild route must proxy the backend response',
    );
  }

  // PA-04: readBody must settle on client abort/close instead of hanging or
  // leaking the partial buffer, and must reject oversized declared bodies.
  {
    const routes = createRouteHarness(defaults);
    const uploadHeaders = {
      ...sameOrigin,
      'content-type': 'image/jpeg',
      'x-photo-filename': 'a.jpg',
    };

    const okReq = fakeRequest({ method: 'POST', url: `${ROUTE_PREFIX}/upload`, headers: uploadHeaders });
    const okRes = fakeResponse();
    const okPending = routes.handler(okReq, okRes);
    okReq.emit('data', Buffer.from([0xff, 0xd8]));
    okReq.emit('end');
    await okPending;
    check(okRes.statusCode === 200 && parseResponseBody(okRes)?.ok === true, 'upload body read must succeed');

    const abortedReq = fakeRequest({ method: 'POST', url: `${ROUTE_PREFIX}/upload`, headers: uploadHeaders });
    abortedReq.complete = false;
    const abortedRes = fakeResponse();
    const abortedPending = routes.handler(abortedReq, abortedRes);
    abortedReq.emit('data', Buffer.from([0xff]));
    abortedReq.emit('aborted');
    await abortedPending;
    check(
      abortedRes.statusCode === 400 && parseResponseBody(abortedRes)?.code === 'request-aborted',
      'client abort must settle readBody with request-aborted',
    );

    const closedReq = fakeRequest({ method: 'POST', url: `${ROUTE_PREFIX}/upload`, headers: uploadHeaders });
    closedReq.complete = false;
    const closedRes = fakeResponse();
    const closedPending = routes.handler(closedReq, closedRes);
    closedReq.emit('close');
    await closedPending;
    check(
      closedRes.statusCode === 400 && parseResponseBody(closedRes)?.code === 'request-aborted',
      'close before complete must settle readBody with request-aborted',
    );

    const bigReq = fakeRequest({
      method: 'POST',
      url: `${ROUTE_PREFIX}/upload`,
      headers: { ...uploadHeaders, 'content-length': String(defaults.maxUploadBytes + 1) },
    });
    const bigRes = fakeResponse();
    await routes.handler(bigReq, bigRes);
    check(
      bigRes.statusCode === 413 && parseResponseBody(bigRes)?.code === 'request-too-large',
      'declared oversized body must be 413',
    );
  }

  // ---------------------------------------------------------------- filenames
  check(contentTypeFor('A.CR3') === 'image/x-canon-cr3', 'content type CR3 failed');
  check(contentTypeFor('a.TIFF') === 'image/tiff', 'content type TIFF failed');
  check(safeFilename('../../etc/passwd') === 'passwd', 'filename basename failed');
  check(!safeFilename('a\u0000b.jpg').includes('\u0000'), 'filename control char failed');

  // ---------------------------------------------------------------- store
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-photo-agent-store-'));
  try {
    const stateFile = path.join(tempRoot, 'state.json');
    const store = createStateStore({ file: stateFile });
    check(store.read() === null, 'empty store must read null');
    store.savePhoto({
      photo_id: 'a'.repeat(32),
      metadata: { width: 1, height: 1, format: 'JPEG' },
      analysis: null,
      style_id: null,
      intensity: 0.7,
    });
    check(store.read()?.photo_id === 'a'.repeat(32), 'photo state write failed');
    store.saveSelection({
      photo_id: 'c'.repeat(32),
      style_id: 'warm',
      intensity: 0.5,
    });
    check(store.read()?.photo_id === 'c'.repeat(32), 'selection must persist its own photo id');
    check(
      store.read()?.metadata === null && store.read()?.analysis === null,
      'selection from another photo must drop stale media',
    );
    store.saveSelection({ photo_id: 'c'.repeat(32), style_id: 'cool' });
    check(store.read()?.style_id === 'cool', 'selection merge failed');
    check(store.read()?.photo_id === 'c'.repeat(32), 'selection merge must keep the photo id');
    store.saveLearning('b'.repeat(32));
    check(store.read()?.learning_session_id === 'b'.repeat(32), 'learning merge failed');
    fs.writeFileSync(stateFile, '{broken', 'utf8');
    const fresh = createStateStore({ file: stateFile });
    check(fresh.read() === null, 'corrupt state must read null');
    fresh.reset();
    check(!fs.existsSync(stateFile), 'reset must remove the state file');
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }

  // ---------------------------------------------------------------- backend
  {
    let spawnCalls = 0;
    let lastArgs = null;
    let lastOptions = null;
    let probeCalls = 0;
    const child = fakeChild();
    const config = loadConfig({
      DSH_PHOTO_AGENT_APP_DIR: path.resolve(root, '..'),
      DSH_PHOTO_AGENT_PORT: '8138',
      DSH_PHOTO_AGENT_START_TIMEOUT_MS: '5000',
      DSH_PHOTO_AGENT_PROVIDER: 'mock',
    });
    const backend = createBackend(config, {
      sleep: async () => {},
      spawn: (command, args, options) => {
        spawnCalls += 1;
        lastArgs = args;
        lastOptions = options;
        return child;
      },
      fetch: async () => {
        probeCalls += 1;
        if (probeCalls === 1) {
          throw Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } });
        }
        return {
          ok: true,
          async json() {
            return {
              plugin_source: path.join(os.tmpdir(), 'ai-photo-agent-fixture', 'AIPhotoAgent.lrplugin'),
              detected: true,
            };
          },
        };
      },
    });
    const status = await backend.ensureReady();
    check(spawnCalls === 1, 'backend should spawn exactly once');
    check(Boolean(lastArgs && lastArgs[1] === '--no-browser'), 'backend must pass --no-browser');
    check(Boolean(lastArgs && lastArgs.includes('8138')), 'backend must pass the configured port');
    check(lastOptions?.env?.AI_PHOTO_AGENT_ANALYSIS_PROVIDER === 'mock', 'backend child env provider mismatch');
    check(status.ready === true && status.managed === true, 'backend status after spawn failed');
    await backend.dispose();
    check(child.killed === true, 'backend dispose must kill the child');
  }

  {
    let spawnCalls = 0;
    const config = loadConfig({
      DSH_PHOTO_AGENT_BASE_URL: 'http://127.0.0.1:9000',
      DSH_PHOTO_AGENT_START_TIMEOUT_MS: '5000',
    });
    const backend = createBackend(config, {
      sleep: async () => {},
      spawn: () => {
        spawnCalls += 1;
        return fakeChild();
      },
      fetch: async () => ({
        ok: true,
        async json() {
          return { plugin_source: 'C:\\x\\AIPhotoAgent.lrplugin', detected: true };
        },
      }),
    });
    const status = await backend.ensureReady();
    check(spawnCalls === 0, 'external backend must not spawn');
    check(status.ready === true && status.managed === false && status.external === true, 'external backend status failed');
    await backend.dispose();
  }

  {
    const config = loadConfig({
      DSH_PHOTO_AGENT_PORT: '8139',
      DSH_PHOTO_AGENT_START_TIMEOUT_MS: '5000',
    });
    const backend = createBackend(config, {
      sleep: async () => {},
      fetch: async () => ({
        ok: true,
        async json() {
          return { hello: 'not our app' };
        },
      }),
    });
    await assertThrowsCode(() => backend.ensureReady(), 'backend-port-conflict');
    await backend.dispose();
  }

  {
    const config = loadConfig({
      DSH_PHOTO_AGENT_PORT: '8140',
      DSH_PHOTO_AGENT_START_TIMEOUT_MS: '5000',
      DSH_PHOTO_AGENT_AUTOSTART: 'false',
    });
    const backend = createBackend(config, {
      sleep: async () => {},
      fetch: async () => {
        throw Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } });
      },
    });
    await assertThrowsCode(() => backend.ensureReady(), 'backend-not-started');
    await backend.dispose();
  }

  // PA-03: a stale managed-ready flag must be cleared by an explicit
  // markUnready() and the next ensureReady() must re-probe instead of trusting
  // the cache. A still-live child must be reused, not duplicated.
  {
    let probeCalls = 0;
    let spawnCalls = 0;
    const child = fakeChild();
    const config = loadConfig({
      DSH_PHOTO_AGENT_PORT: '8141',
      DSH_PHOTO_AGENT_START_TIMEOUT_MS: '5000',
      DSH_PHOTO_AGENT_PROVIDER: 'mock',
    });
    const backend = createBackend(config, {
      sleep: async () => {},
      spawn: () => {
        spawnCalls += 1;
        return child;
      },
      fetch: async () => {
        probeCalls += 1;
        if (probeCalls === 1) {
          throw Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } });
        }
        return {
          ok: true,
          async json() {
            return { plugin_source: 'C:\\x\\AIPhotoAgent.lrplugin', detected: true };
          },
        };
      },
    });
    await backend.ensureReady();
    check(spawnCalls === 1 && probeCalls === 2, 'managed backend must start exactly once');
    backend.markUnready();
    const recovered = await backend.ensureReady();
    check(probeCalls === 3, 'markUnready must force a readiness re-probe');
    check(
      spawnCalls === 1 && recovered.ready === true && recovered.managed === true,
      'readiness recovery must reuse the live child instead of respawning',
    );
    await backend.dispose();
    await backend.dispose();
    check(child.killed === true, 'dispose must be idempotent and still kill the child');
  }

  // PA-01/PA-02: a missed exit event leaves a dead child handle; ensureReady
  // must drop it and spawn a fresh backend instead of waiting for the timeout.
  {
    let probeCalls = 0;
    let spawnCalls = 0;
    const children = [];
    const config = loadConfig({
      DSH_PHOTO_AGENT_PORT: '8142',
      DSH_PHOTO_AGENT_START_TIMEOUT_MS: '5000',
      DSH_PHOTO_AGENT_PROVIDER: 'mock',
    });
    const backend = createBackend(config, {
      sleep: async () => {},
      spawn: () => {
        spawnCalls += 1;
        const child = fakeChild();
        children.push(child);
        return child;
      },
      fetch: async () => {
        probeCalls += 1;
        const ok = probeCalls !== 1 && probeCalls !== 3;
        if (!ok) {
          throw Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } });
        }
        return {
          ok: true,
          async json() {
            return { plugin_source: 'C:\\x\\AIPhotoAgent.lrplugin', detected: true };
          },
        };
      },
    });
    await backend.ensureReady();
    check(spawnCalls === 1 && probeCalls === 2, 'initial managed start must succeed');
    // Simulate a missed 'exit' event: the handle still exists but is dead.
    children[0].exitCode = 1;
    backend.markUnready();
    const respawned = await backend.ensureReady();
    check(spawnCalls === 2, 'dead child handle must be replaced');
    check(respawned.ready === true && respawned.managed === true, 'respawned backend must be ready');
    await backend.dispose();
  }

  // PA-02: dispose must await the real 'exit' event (bounded), not sleep blindly.
  {
    let probeCalls = 0;
    let spawnCalls = 0;
    const child = fakeChild();
    child.kill = () => {
      child.killed = true;
      setTimeout(() => {
        child.exitCode = 0;
        child.emit('exit', 0, null);
      }, 20);
      return true;
    };
    const config = loadConfig({
      DSH_PHOTO_AGENT_PORT: '8143',
      DSH_PHOTO_AGENT_START_TIMEOUT_MS: '5000',
      DSH_PHOTO_AGENT_PROVIDER: 'mock',
    });
    const backend = createBackend(config, {
      sleep: async () => {},
      spawn: () => {
        spawnCalls += 1;
        return child;
      },
      fetch: async () => {
        probeCalls += 1;
        if (probeCalls === 1) {
          throw Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } });
        }
        return {
          ok: true,
          async json() {
            return { plugin_source: 'C:\\x\\AIPhotoAgent.lrplugin', detected: true };
          },
        };
      },
    });
    await backend.ensureReady();
    const startedAt = Date.now();
    await backend.dispose();
    const elapsed = Date.now() - startedAt;
    check(spawnCalls === 1 && child.killed === true, 'dispose must request child termination');
    check(elapsed < 1500, `dispose must wait for the exit event, not a fixed sleep (${elapsed}ms)`);
  }

  // ---------------------------------------------------------------- service recovery
  // PA-16: a managed-ready operation that hits ECONNREFUSED re-probes and
  // retries once; timeouts are never replayed (the request may have run).
  {
    let attempts = 0;
    let ensureCalls = 0;
    let markCalls = 0;
    const config = loadConfig({});
    const backend = {
      ensureReady: async () => {
        ensureCalls += 1;
        return { ready: true, managed: true };
      },
      status: () => ({ ready: true, managed: true }),
      markUnready: () => {
        markCalls += 1;
      },
      dispose: async () => {},
    };
    const fetchImpl = async () => {
      attempts += 1;
      if (attempts === 1) {
        throw Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } });
      }
      return {
        ok: true,
        status: 200,
        async text() {
          return JSON.stringify({ detected: true, plugin_source: 'C:\\x\\AIPhotoAgent.lrplugin' });
        },
      };
    };
    const service = createPhotoService({ config, backend, store: {}, fetch: fetchImpl });
    const lightroom = await service.lightroomStatus();
    check(lightroom.detected === true && attempts === 2, 'connection-refused failure must retry once');
    check(markCalls === 1 && ensureCalls === 2, 'retry must re-probe readiness');
  }
  {
    let attempts = 0;
    const config = loadConfig({});
    const backend = {
      ensureReady: async () => ({ ready: true, managed: true }),
      status: () => ({ ready: true, managed: true }),
      markUnready: () => {},
      dispose: async () => {},
    };
    const service = createPhotoService({
      config,
      backend,
      store: {},
      fetch: async () => {
        attempts += 1;
        throw Object.assign(new Error('aborted'), { name: 'TimeoutError' });
      },
    });
    await assertThrowsCode(() => service.lightroomStatus(), 'backend-timeout');
    check(attempts === 1, 'timeouts must not be replayed');
  }

  if (failures.length > 0) {
    console.error('host check failed:');
    for (const failure of failures) console.error(` - ${failure}`);
    process.exit(1);
  }
  console.log('host checks OK: config, assets, errors, routes, store, backend lifecycle, service recovery');
}

main().catch((error) => {
  console.error(`host check crashed: ${error.stack || error.message}`);
  process.exit(1);
});
