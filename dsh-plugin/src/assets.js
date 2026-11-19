/**
 * Asset path validation for the binary proxy.
 *
 * The plugin's `/asset` route may only fetch the AI Photo Agent application's
 * own public endpoints: generated previews, exported XMP files, upload display
 * images, and learning reference images. It must never become an arbitrary
 * local-file reader.
 *
 * The returned value is always a canonical URL pathname produced by the WHATWG
 * URL parser, and every caller must fetch *that* value rather than the raw
 * input (otherwise percent-encoded dot segments could be re-interpreted by a
 * later `new URL()`/`fetch()` and escape the allowlist).
 *
 * @module @local/dsh-photo-agent/assets
 */

import path from 'node:path';

const STATIC_PREFIXES = ['/generated/', '/exports/'];

const API_PATTERNS = [
  /^\/api\/photos\/[0-9a-f]{32}\/display$/,
  /^\/api\/learning\/sessions\/[0-9a-f]{32}\/references\/[0-9a-f]{32}\/image$/,
];

const MAX_PATH_LENGTH = 2048;
const MAX_DECODE_ROUNDS = 4;
const SYNTHETIC_ORIGIN = 'http://asset.invalid';

/** Absolute paths and protocol-relative forms are never asset paths. */
const SCHEME_PREFIX = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

/**
 * Percent-decode a path until it stops changing, within a bounded number of
 * rounds. Double/triple-encoded traversal decodes to a literal dot segment here
 * instead of surviving as `%2e%2e` for the URL parser to expand later.
 *
 * @param {string} value
 * @returns {string | null}
 */
function decodeFully(value) {
  let current = value;
  for (let round = 0; round < MAX_DECODE_ROUNDS; round += 1) {
    let decoded;
    try {
      decoded = decodeURIComponent(current);
    } catch {
      return null;
    }
    if (decoded === current) return current;
    current = decoded;
    if (current.length > MAX_PATH_LENGTH) return null;
  }
  // Still changing after the bound: more encoding layers than any real asset
  // URL can legitimately have. Refuse instead of guessing.
  return null;
}

function hasDotSegment(pathname) {
  return pathname.split('/').some((segment) => segment === '.' || segment === '..');
}

/**
 * Decode and normalize a backend-relative asset path, or return null.
 *
 * @param {unknown} raw
 * @returns {string | null}
 */
export function normalizeAssetPath(raw) {
  if (typeof raw !== 'string') return null;
  let value = raw.trim();
  if (!value || value.length > MAX_PATH_LENGTH) return null;
  if (/[\u0000-\u001f\u007f]/.test(value)) return null;

  const decoded = decodeFully(value);
  if (decoded === null) return null;
  value = decoded;

  if (value.length > MAX_PATH_LENGTH) return null;
  if (/[\u0000-\u001f\u007f]/.test(value)) return null;
  // No residual percent escape may reach the URL parser. The application's own
  // URL helpers never emit literal percent signs in these paths.
  if (value.includes('%') || value.includes('\\')) return null;
  if (!value.startsWith('/') || value.startsWith('//')) return null;
  if (SCHEME_PREFIX.test(value)) return null;

  let parsed;
  try {
    parsed = new URL(value, SYNTHETIC_ORIGIN);
  } catch {
    return null;
  }
  // A bare path must stay on the synthetic origin: absolute URLs,
  // protocol-relative forms and user-info tricks all fail this check.
  if (parsed.origin !== SYNTHETIC_ORIGIN) return null;
  if (parsed.username || parsed.password) return null;
  if (parsed.search || parsed.hash) return null;

  const pathname = parsed.pathname;
  if (!pathname.startsWith('/') || pathname.length > MAX_PATH_LENGTH) return null;
  // Reject dot segments both before and after URL normalisation. The parser
  // silently removes them; checking the result alone would hide the attempt.
  if (hasDotSegment(value) || hasDotSegment(pathname)) return null;
  if (pathname.includes('%') || pathname.includes('\\')) return null;
  return pathname;
}

/**
 * Whether the path belongs to the application's public asset surface.
 *
 * @param {unknown} raw
 */
export function isAllowedAssetPath(raw) {
  const value = normalizeAssetPath(raw);
  if (!value) return false;
  if (STATIC_PREFIXES.some((prefix) => value.startsWith(prefix))) return true;
  return API_PATTERNS.some((pattern) => pattern.test(value));
}

/**
 * Resolve a generated/exported asset URL to an absolute local file path.
 *
 * Display and learning images are served by the backend and do not have a
 * stable local path here; this helper returns null for them.
 *
 * @param {string} dataRoot
 * @param {unknown} raw
 * @returns {string | null}
 */
export function assetAbsolutePath(dataRoot, raw) {
  const value = normalizeAssetPath(raw);
  if (!value) return null;
  let relative;
  if (value.startsWith('/generated/')) {
    relative = path.join('generated', 'previews', value.slice('/generated/'.length));
  } else if (value.startsWith('/exports/')) {
    relative = path.join('exports', value.slice('/exports/'.length));
  } else {
    return null;
  }
  const root = path.resolve(dataRoot);
  const target = path.resolve(root, relative);
  if (target !== root && !target.startsWith(root + path.sep)) return null;
  return target;
}
