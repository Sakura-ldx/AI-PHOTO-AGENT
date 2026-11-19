/**
 * Host-side configuration for the AI Photo Agent plugin.
 *
 * Every value can be overridden with a DSH_PHOTO_AGENT_* environment variable
 * on the Harness process. Defaults point at this repository's own Python
 * checkout and its FastAPI backend.
 *
 * @module @local/dsh-photo-agent/config
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** Package root (`dsh-plugin`). */
export const PACKAGE_ROOT = path.resolve(HERE, '..');

/** Repository root that contains `run_app.py`. */
export const DEFAULT_APP_DIR = path.resolve(PACKAGE_ROOT, '..');

/** Backend port shared with the bundled Lightroom plugin's Config.lua. */
export const DEFAULT_PORT = 8000;

/** Same upload cap the Python application enforces. */
export const DEFAULT_MAX_UPLOAD_BYTES = 256 * 1024 * 1024;

const PROVIDERS = new Set(['deepseek', 'mock', 'openai', 'codex']);
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);

function stringEnv(env, name) {
  const raw = env[name];
  if (raw === undefined || raw === null) return undefined;
  const value = String(raw).trim();
  return value || undefined;
}

function intEnv(env, name, fallback, minimum, maximum) {
  const raw = stringEnv(env, name);
  if (raw === undefined) return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value)) {
    throw new Error(`${name} 必须是整数。`);
  }
  return Math.min(maximum, Math.max(minimum, value));
}

function boolEnv(env, name, fallback) {
  const raw = stringEnv(env, name);
  if (raw === undefined) return fallback;
  return !['0', 'false', 'no', 'off'].includes(raw.toLowerCase());
}

function resolvePath(value, fallback) {
  if (!value) return fallback;
  return path.resolve(value);
}

function defaultPython(appDir) {
  const candidates =
    process.platform === 'win32'
      ? [path.join(appDir, '.venv', 'Scripts', 'python.exe')]
      : [path.join(appDir, '.venv', 'bin', 'python')];
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {
      /* keep looking */
    }
  }
  return 'python';
}

function normalizeBaseUrl(raw, port) {
  const value = raw ? String(raw).trim() : '';
  if (!value) return `http://127.0.0.1:${port}`;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('DSH_PHOTO_AGENT_BASE_URL 必须是 http(s) URL。');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('DSH_PHOTO_AGENT_BASE_URL 只支持 http 或 https。');
  }
  if (!LOOPBACK_HOSTS.has(parsed.hostname)) {
    throw new Error('DSH_PHOTO_AGENT_BASE_URL 只能指向本机回环地址。');
  }
  return value.replace(/\/+$/, '');
}

/**
 * Resolve all plugin settings from an environment map.
 *
 * @param {NodeJS.ProcessEnv | Record<string, string | undefined>} [env]
 * @returns {object}
 */
export function loadConfig(env = process.env) {
  const appDir = resolvePath(stringEnv(env, 'DSH_PHOTO_AGENT_APP_DIR'), DEFAULT_APP_DIR);
  const python = stringEnv(env, 'DSH_PHOTO_AGENT_PYTHON') || defaultPython(appDir);
  const port = intEnv(env, 'DSH_PHOTO_AGENT_PORT', DEFAULT_PORT, 1, 65535);
  const externalBaseUrl = normalizeExternal(env, port);
  const baseUrl = externalBaseUrl || `http://127.0.0.1:${port}`;
  const provider = (stringEnv(env, 'DSH_PHOTO_AGENT_PROVIDER') || 'deepseek').toLowerCase();
  if (!PROVIDERS.has(provider)) {
    throw new Error('DSH_PHOTO_AGENT_PROVIDER 必须是 deepseek、mock、openai 或 codex。');
  }
  const dataDir = stringEnv(env, 'DSH_PHOTO_AGENT_DATA_DIR');
  const lightroomRoot = stringEnv(env, 'DSH_PHOTO_AGENT_LIGHTROOM_ROOT');
  const dataRoot = dataDir ? path.resolve(dataDir) : appDir;
  const autoStart = boolEnv(env, 'DSH_PHOTO_AGENT_AUTOSTART', true);

  const childEnv = {
    ...process.env,
    AI_PHOTO_AGENT_ANALYSIS_PROVIDER: provider,
    AI_PHOTO_AGENT_SERVER_URL: baseUrl,
  };
  if (dataDir) childEnv.AI_PHOTO_AGENT_DATA_DIR = path.resolve(dataDir);
  if (lightroomRoot) childEnv.AI_PHOTO_AGENT_LIGHTROOM_ROOT = path.resolve(lightroomRoot);
  childEnv.PYTHONUNBUFFERED = '1';

  return {
    appDir,
    python,
    port,
    baseUrl,
    externalBaseUrl,
    autoStart,
    provider,
    dataDir: dataDir ? path.resolve(dataDir) : undefined,
    dataRoot,
    lightroomRoot: lightroomRoot ? path.resolve(lightroomRoot) : undefined,
    analysisTimeoutMs: intEnv(
      env,
      'DSH_PHOTO_AGENT_ANALYSIS_TIMEOUT_MS',
      240_000,
      10_000,
      900_000,
    ),
    startTimeoutMs: intEnv(env, 'DSH_PHOTO_AGENT_START_TIMEOUT_MS', 60_000, 5_000, 300_000),
    maxUploadBytes: intEnv(
      env,
      'DSH_PHOTO_AGENT_MAX_UPLOAD_BYTES',
      DEFAULT_MAX_UPLOAD_BYTES,
      1024 * 1024,
      512 * 1024 * 1024,
    ),
    stateFile: path.join(dataRoot, 'data', 'runtime', 'dsh-photo-agent', 'state.json'),
    logLimit: 200,
    childEnv,
  };
}

function normalizeExternal(env, port) {
  const raw = stringEnv(env, 'DSH_PHOTO_AGENT_BASE_URL');
  if (!raw) return null;
  return normalizeBaseUrl(raw, port);
}

/** Public (secret-free) configuration for `/state` and logs. */
export function publicConfig(config) {
  return {
    app_dir: config.appDir,
    python: config.python,
    port: config.port,
    base_url: config.baseUrl,
    external: Boolean(config.externalBaseUrl),
    auto_start: config.autoStart,
    provider: config.provider,
    data_root: config.dataRoot,
    max_upload_bytes: config.maxUploadBytes,
    analysis_timeout_ms: config.analysisTimeoutMs,
    start_timeout_ms: config.startTimeoutMs,
  };
}
