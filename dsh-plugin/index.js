/**
 * Host half of the AI Photo Agent plugin.
 *
 * Registers five agent tools, the panel's local `/photo-agent/api` routes, and
 * the lifecycle manager for the existing FastAPI backend. Rendering lives in
 * `client.js`; all photo/XMP/Lightroom behavior stays in the Python project.
 *
 * @module @local/dsh-photo-agent
 */

import { createBackend } from './src/backend.js';
import { loadConfig } from './src/config.js';
import { errorMessage } from './src/errors.js';
import { createRouteHandler, ROUTE_PREFIX } from './src/routes.js';
import { createPhotoService } from './src/service.js';
import { createStateStore } from './src/store.js';
import { registerTools } from './src/tools.js';

export const inject = ['tools', 'webServer'];

/** Both services are hard requirements; the plugin has no useful half-life. */
export function apply(ctx) {
  let config;
  try {
    config = loadConfig();
  } catch (error) {
    ctx.logger?.error?.(`[photo-agent] 配置无效：${errorMessage(error)}`);
    return;
  }

  const store = createStateStore({ file: config.stateFile, logger: ctx.logger });
  const backend = createBackend(config, { logger: ctx.logger });
  const service = createPhotoService({
    config,
    backend,
    store,
    logger: ctx.logger,
  });

  ctx.logger?.info?.(
    `[photo-agent] 插件已加载；后端 ${config.baseUrl}（${config.externalBaseUrl ? '外部' : '托管'}），数据根目录 ${config.dataRoot}`,
  );

  ctx.effect(() => {
    let disposed = false;
    let retry = null;
    const cleanups = [];

    const boot = () => {
      if (disposed) return;
      const tools = ctx.get('tools');
      const webServer = ctx.get('webServer');
      if (!tools || !webServer) {
        retry = setTimeout(boot, 250);
        return;
      }

      try {
        cleanups.push(...registerTools(ctx, { service, config }));
      } catch (error) {
        ctx.logger?.error?.(`[photo-agent] 工具注册失败：${errorMessage(error)}`);
      }

      try {
        cleanups.push(
          webServer.register({
            kind: 'prefix',
            path: ROUTE_PREFIX,
            handler: createRouteHandler({ service, config, logger: ctx.logger }),
          }),
        );
      } catch (error) {
        ctx.logger?.error?.(`[photo-agent] 路由注册失败：${errorMessage(error)}`);
      }
    };

    boot();

    return async () => {
      disposed = true;
      if (retry) clearTimeout(retry);
      for (const dispose of cleanups) {
        try {
          dispose();
        } catch {
          /* teardown is best effort */
        }
      }
      // Await the real child exit instead of fire-and-forget: a Host reload
      // must not leave an orphan uvicorn process behind. Bounded by
      // backend.stopChild() (~2s + one tree-kill fallback).
      try {
        await backend.dispose();
      } catch (error) {
        ctx.logger?.warn?.(`[photo-agent] 关闭后端失败：${errorMessage(error)}`);
      }
    };
  }, 'photo-agent: tools, routes and backend lifecycle');
}
