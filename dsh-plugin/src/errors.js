/**
 * Errors shared by the Host half.
 *
 * A PhotoError carries the HTTP status the plugin route should return and a
 * stable machine code, while its message is user-facing Chinese text.
 *
 * @module @local/dsh-photo-agent/errors
 */

export class PhotoError extends Error {
  /**
   * @param {string} message
   * @param {{ status?: number, code?: string, detail?: string, cause?: unknown }} [options]
   */
  constructor(message, options = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = 'PhotoError';
    this.status = Number.isInteger(options.status) ? options.status : 500;
    this.code = options.code || 'photo-agent-error';
    this.detail = options.detail;
  }
}

export function errorMessage(error) {
  if (error instanceof Error) return error.message;
  return String(error);
}

export function isAbortError(error) {
  return Boolean(
    error &&
      (error.name === 'AbortError' ||
        error.name === 'TimeoutError' ||
        error.code === 'ABORT_ERR' ||
        error.code === 'ETIMEDOUT'),
  );
}

export function isConnectionRefused(error) {
  const code = error && error.cause && error.cause.code;
  if (code === 'ECONNREFUSED') return true;
  const message = errorMessage(error);
  return (
    message.includes('ECONNREFUSED') ||
    message.includes('connect ECONNREFUSED') ||
    message.includes('fetch failed') && message.includes('refused')
  );
}

/**
 * Convert a fetch failure into an actionable PhotoError.
 *
 * @param {unknown} error
 * @param {string} baseUrl
 * @param {string} [action]
 */
export function networkError(error, baseUrl, action = '连接后端') {
  if (isAbortError(error)) {
    return new PhotoError(`${action}超时：后端可能仍在分析，请稍后重试。`, {
      status: 504,
      code: 'backend-timeout',
      detail: errorMessage(error),
    });
  }
  if (isConnectionRefused(error)) {
    return new PhotoError(
      `无法${action}：${baseUrl} 没有响应。请确认本机 AI Photo Agent 后端已启动，或在插件环境变量中调整 DSH_PHOTO_AGENT_PORT / DSH_PHOTO_AGENT_BASE_URL。`,
      {
        status: 503,
        code: 'backend-unreachable',
        detail: errorMessage(error),
      },
    );
  }
  return new PhotoError(`无法${action}：${errorMessage(error)}`, {
    status: 503,
    code: 'backend-unreachable',
    detail: errorMessage(error),
  });
}

/**
 * Convert a non-2xx backend response into a PhotoError preserving the HTTP
 * status and the backend's own detail message.
 *
 * @param {number} status
 * @param {any} payload
 * @param {string} fallback
 */
export function upstreamError(status, payload, fallback) {
  const detail = payload && typeof payload === 'object' ? payload.detail : undefined;
  let message = fallback;
  if (typeof detail === 'string' && detail.trim()) {
    message = detail.trim();
  } else if (detail && typeof detail === 'object') {
    message = JSON.stringify(detail);
  }
  return new PhotoError(message, {
    status: Number.isInteger(status) && status >= 400 ? status : 502,
    code: 'backend-error',
    detail: typeof detail === 'string' ? detail : undefined,
  });
}
