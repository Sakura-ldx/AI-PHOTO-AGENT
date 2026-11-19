/**
 * Lifecycle manager for the local AI Photo Agent FastAPI backend.
 *
 * The plugin never reimplements image analysis. It reuses an already running
 * application when one answers on the configured loopback address, otherwise
 * starts this checkout's `run_app.py` with the project virtualenv.
 *
 * @module @local/dsh-photo-agent/backend
 */

import { spawn as defaultSpawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { PhotoError, errorMessage, isAbortError, isConnectionRefused } from './errors.js';

const PROBE_TIMEOUT_MS = 1500;
const PROBE_INTERVAL_MS = 300;
const EXIT_WAIT_MS = 2000;
const TREE_KILL_TIMEOUT_MS = 1500;

/**
 * @param {ReturnType<import('./config.js').loadConfig>} config
 * @param {{
 *   spawn?: typeof defaultSpawn,
 *   fetch?: typeof fetch,
 *   sleep?: (ms: number) => Promise<void>,
 *   logger?: { info?: Function, warn?: Function, error?: Function },
 * }} [deps]
 */
export function createBackend(config, deps = {}) {
  const spawnImpl = deps.spawn || defaultSpawn;
  const fetchImpl = deps.fetch || globalThis.fetch;
  const sleep = deps.sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const logger = deps.logger;

  /** @type {import('node:child_process').ChildProcess | null} */
  let child = null;
  let managed = false;
  let ready = false;
  let starting = null;
  let disposed = false;
  let lastError = null;
  let spawnFailure = null;
  const logs = [];

  function log(line) {
    const text = String(line === undefined || line === null ? '' : line).replace(/\s+$/, '');
    if (!text) return;
    logs.push(text);
    if (logs.length > config.logLimit) logs.splice(0, logs.length - config.logLimit);
  }

  function append(chunk) {
    for (const line of String(chunk).split(/\r?\n/)) log(line);
  }

  function logTail(limit = 40) {
    return logs.slice(Math.max(0, logs.length - limit)).join('\n');
  }

  function status() {
    return {
      ready,
      managed,
      base_url: config.baseUrl,
      port: config.port,
      python: config.python,
      app_dir: config.appDir,
      provider: config.provider,
      external: Boolean(config.externalBaseUrl),
      auto_start: config.autoStart,
      last_error: lastError,
      log_tail: logTail(),
    };
  }

  async function probe() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
    try {
      const response = await fetchImpl(`${config.baseUrl}/api/lightroom/status`, {
        method: 'GET',
        headers: { accept: 'application/json' },
        signal: controller.signal,
      });
      if (!response.ok) {
        return { ok: false, kind: 'foreign', reason: `HTTP ${response.status}` };
      }
      let body;
      try {
        body = await response.json();
      } catch {
        return { ok: false, kind: 'foreign', reason: '响应不是 JSON' };
      }
      const source = body && body.plugin_source;
      if (typeof source !== 'string' || !source.endsWith('AIPhotoAgent.lrplugin')) {
        return { ok: false, kind: 'foreign', reason: '响应不是 AI Photo Agent' };
      }
      return { ok: true, body };
    } catch (error) {
      if (isAbortError(error)) return { ok: false, kind: 'timeout', reason: errorMessage(error) };
      if (isConnectionRefused(error)) return { ok: false, kind: 'absent', reason: errorMessage(error) };
      return { ok: false, kind: 'error', reason: errorMessage(error) };
    } finally {
      clearTimeout(timer);
    }
  }

  function spawnChild() {
    if (config.externalBaseUrl) {
      throw new PhotoError(
        `外部后端 ${config.baseUrl} 不可用，且已配置 DSH_PHOTO_AGENT_BASE_URL，插件不会自行启动进程。`,
        { status: 503, code: 'backend-unreachable' },
      );
    }
    if (!config.autoStart) {
      throw new PhotoError(
        `后端未启动，且 DSH_PHOTO_AGENT_AUTOSTART=false。请先运行 AI Photo Agent，或打开自动启动。`,
        { status: 503, code: 'backend-not-started' },
      );
    }
    const script = path.join(config.appDir, 'run_app.py');
    if (!fs.existsSync(script)) {
      throw new PhotoError(
        `找不到后端入口 ${script}。请检查 DSH_PHOTO_AGENT_APP_DIR 是否指向 AI Photo Agent 仓库。`,
        { status: 503, code: 'backend-script-missing' },
      );
    }
    if (config.python.includes(path.sep) && !fs.existsSync(config.python)) {
      throw new PhotoError(
        `找不到 Python 解释器 ${config.python}。请创建 .venv 或设置 DSH_PHOTO_AGENT_PYTHON。`,
        { status: 503, code: 'python-missing' },
      );
    }

    const args = [script, '--no-browser', '--port', String(config.port)];
    log(`[spawn] ${config.python} ${args.map((item) => (item.includes(' ') ? `"${item}"` : item)).join(' ')}`);
    spawnFailure = null;
    lastError = null;

    let spawned;
    try {
      spawned = spawnImpl(config.python, args, {
        cwd: config.appDir,
        env: config.childEnv,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      throw new PhotoError(`无法启动 Python 后端：${errorMessage(error)}`, {
        status: 503,
        code: 'backend-spawn-failed',
        detail: errorMessage(error),
      });
    }

    child = spawned;
    managed = true;
    ready = false;
    spawned.stdout?.on?.('data', append);
    spawned.stderr?.on?.('data', append);
    spawned.on?.('error', (error) => {
      spawnFailure = `无法启动 Python 后端：${errorMessage(error)}`;
      lastError = spawnFailure;
      log(`[error] ${spawnFailure}`);
    });
    spawned.on?.('exit', (code, signal) => {
      log(`[exit] code=${code} signal=${signal}`);
      if (child !== spawned) return;
      child = null;
      ready = false;
      managed = false;
      if (!disposed) {
        lastError = `后端进程已退出（code=${code}${signal ? `, signal=${signal}` : ''}）。`;
      }
    });
  }

  /**
   * Wait for a child to really exit, racing a bounded timeout.
   *
   * Starting this wait *before* sending the signal avoids the classic race
   * where the process exits between `kill()` and the listener registration.
   *
   * @param {import('node:child_process').ChildProcess | null} target
   * @param {number} ms
   * @returns {Promise<boolean>} true when the child exited in time
   */
  function waitForExit(target, ms) {
    return new Promise((resolve) => {
      if (!target || target.exitCode !== null || target.signalCode !== null) {
        resolve(true);
        return;
      }
      let done = false;
      const finish = (value) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        target.off?.('exit', onExit);
        target.off?.('error', onExit);
        resolve(value);
      };
      const onExit = () => finish(true);
      const timer = setTimeout(() => finish(false), ms);
      if (typeof timer.unref === 'function') timer.unref();
      target.once?.('exit', onExit);
      target.once?.('error', onExit);
    });
  }

  /**
   * Kill the whole process tree. On Windows the Python backend may spawn
   * grandchildren during startup; `taskkill /T` reaps them, which plain
   * `child.kill()` does not.
   *
   * @param {import('node:child_process').ChildProcess | null} target
   * @returns {boolean} whether a kill command was issued successfully
   */
  function treeKill(target) {
    if (!target || !target.pid) return false;
    if (process.platform === 'win32') {
      try {
        const result = spawnSync('taskkill', ['/PID', String(target.pid), '/T', '/F'], {
          windowsHide: true,
          timeout: TREE_KILL_TIMEOUT_MS,
          stdio: 'ignore',
        });
        return result.status === 0;
      } catch {
        return false;
      }
    }
    try {
      return target.kill?.('SIGKILL') === true;
    } catch {
      return false;
    }
  }

  async function stopChild() {
    const target = child;
    child = null;
    ready = false;
    managed = false;
    if (!target) return;
    const exited = waitForExit(target, EXIT_WAIT_MS);
    try {
      target.kill?.();
    } catch {
      /* best effort */
    }
    if (await exited) return;
    if (treeKill(target)) await waitForExit(target, EXIT_WAIT_MS);
  }

  /**
   * Last-resort synchronous cleanup if the Host process exits without running
   * the plugin's async disposer (crash, forced shutdown).
   */
  function killChildOnExit() {
    const target = child;
    if (!target || target.exitCode !== null || target.signalCode !== null) return;
    try {
      target.kill?.();
    } catch {
      /* best effort */
    }
    if (process.platform === 'win32' && target.pid) {
      try {
        spawnSync('taskkill', ['/PID', String(target.pid), '/T', '/F'], {
          windowsHide: true,
          timeout: TREE_KILL_TIMEOUT_MS,
          stdio: 'ignore',
        });
      } catch {
        /* best effort */
      }
    }
  }

  async function waitReady() {
    const deadline = Date.now() + config.startTimeoutMs;
    while (!disposed && Date.now() < deadline) {
      if (spawnFailure) {
        await stopChild();
        throw new PhotoError(spawnFailure, {
          status: 503,
          code: 'backend-spawn-failed',
          detail: logTail(),
        });
      }
      const result = await probe();
      if (result.ok) {
        ready = true;
        lastError = null;
        logger?.info?.(`[photo-agent] 后端已就绪：${config.baseUrl}`);
        return status();
      }
      if (!child) {
        await stopChild();
        throw new PhotoError(
          `后端启动失败：${lastError || '进程已退出'}。${logTail(8) ? `\n${logTail(8)}` : ''}`,
          { status: 503, code: 'backend-start-failed', detail: logTail() },
        );
      }
      await sleep(PROBE_INTERVAL_MS);
    }
    await stopChild();
    throw new PhotoError(
      `后端在 ${Math.round(config.startTimeoutMs / 1000)} 秒内未就绪。${logTail(8) ? `\n${logTail(8)}` : ''}`,
      { status: 503, code: 'backend-start-timeout', detail: logTail() },
    );
  }

  async function runEnsure() {
    const first = await probe();
    if (first.ok) {
      ready = true;
      lastError = null;
      return status();
    }
    if (config.externalBaseUrl) {
      throw new PhotoError(
        `无法连接外部后端 ${config.baseUrl}：${first.reason}。请确认该地址上的 AI Photo Agent 已启动。`,
        { status: 503, code: 'backend-unreachable' },
      );
    }
    if (first.kind === 'foreign') {
      throw new PhotoError(
        `端口 ${config.port} 已被其他服务占用（${first.reason}），插件不会结束别人的进程。请设置 DSH_PHOTO_AGENT_PORT 换端口，并同步 Lightroom 插件 Config.lua。`,
        { status: 503, code: 'backend-port-conflict' },
      );
    }
    if (first.kind === 'timeout') {
      throw new PhotoError(
        `探测 ${config.baseUrl} 超时，端口可能被占用或服务正在启动。请稍后重试。`,
        { status: 503, code: 'backend-probe-timeout' },
      );
    }
    if (!child) spawnChild();
    return waitReady();
  }

  /**
   * Drop the cached ready flag. The next `ensureReady()` re-probes (and, when
   * the managed child is still alive, reuses it instead of spawning a second
   * backend). Called by the service after a connection failure so a stale
   * `ready=true` cannot turn into a sticky 503.
   */
  function markUnready() {
    ready = false;
  }

  async function ensureReady() {
    if (disposed) {
      throw new PhotoError('插件已卸载。', { status: 503, code: 'plugin-disposed' });
    }
    if (child && (child.exitCode !== null || child.signalCode !== null)) {
      // The exit event was missed (e.g. the process was killed externally):
      // drop the stale handle so runEnsure() probes and respawns instead of
      // waiting for a dead child until the start timeout.
      child = null;
      managed = false;
      ready = false;
    }
    if (ready && managed) return status();
    if (ready && !managed) {
      // An external instance can disappear without an exit event. Re-probe so
      // one request after the instance dies recovers instead of failing twice.
      const current = await probe();
      if (current.ok) return status();
      ready = false;
    }
    if (starting) return starting;
    starting = runEnsure().finally(() => {
      starting = null;
    });
    return starting;
  }

  async function dispose() {
    disposed = true;
    process.removeListener('exit', killChildOnExit);
    await stopChild();
  }

  process.once('exit', killChildOnExit);

  return { ensureReady, status, dispose, probe, logTail, markUnready };
}
