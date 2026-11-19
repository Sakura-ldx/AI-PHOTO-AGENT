/**
 * Small durable cache for the last photo/analysis shown by the panel.
 *
 * The Python application's session files remain the source of truth. This file
 * only lets the panel restore its last view after the sidebar selection changes
 * or the page is refreshed. Corruption or an unwritable directory degrades to
 * "no last result" and never blocks the main workflow.
 *
 * @module @local/dsh-photo-agent/store
 */

import fs from 'node:fs';
import path from 'node:path';

const SCHEMA_VERSION = 1;

export function createStateStore({ file, logger } = {}) {
  let cached;

  function read() {
    if (cached !== undefined) return cached;
    try {
      const text = fs.readFileSync(file, 'utf8');
      const value = JSON.parse(text);
      if (!value || typeof value !== 'object' || value.schema_version !== SCHEMA_VERSION) {
        cached = null;
        return null;
      }
      cached = value;
      return value;
    } catch {
      cached = null;
      return null;
    }
  }

  function write(value) {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const temp = `${file}.${process.pid}.${Date.now()}.tmp`;
      fs.writeFileSync(temp, JSON.stringify(value, null, 2), 'utf8');
      fs.renameSync(temp, file);
      return true;
    } catch (error) {
      logger?.warn?.(`[photo-agent] 无法写入最近结果缓存：${error instanceof Error ? error.message : String(error)}`);
      return false;
    }
  }

  function update(patch) {
    const next = {
      ...(read() || {}),
      ...patch,
      schema_version: SCHEMA_VERSION,
      updated_at: new Date().toISOString(),
    };
    write(next);
    cached = next;
    return next;
  }

  function savePhoto({ photo_id, metadata, analysis, style_id, intensity }) {
    const previous = read() || {};
    return update({
      photo_id,
      metadata: metadata === undefined ? previous.metadata || null : metadata,
      analysis: analysis === undefined ? previous.analysis || null : analysis,
      style_id: style_id === undefined ? previous.style_id || null : style_id,
      intensity: intensity === undefined ? previous.intensity ?? 1.0 : intensity,
    });
  }

  function saveSelection({ photo_id, style_id, intensity }) {
    const previous = read() || {};
    const patch = {};
    if (style_id !== undefined) patch.style_id = style_id;
    if (intensity !== undefined) patch.intensity = intensity;
    if (photo_id !== undefined) {
      // A selection is only meaningful for the photo it was produced for.
      // Persist the id with it, and drop cached media that belonged to another
      // photo so a restore can never pair photo A with photo B's analysis.
      patch.photo_id = photo_id;
      if (previous.photo_id !== photo_id) {
        patch.metadata = null;
        patch.analysis = null;
      }
    }
    return update(patch);
  }

  function saveLearning(session_id) {
    return update({ learning_session_id: session_id });
  }

  function reset() {
    cached = null;
    try {
      fs.unlinkSync(file);
    } catch {
      /* already absent */
    }
  }

  return { read, update, savePhoto, saveSelection, saveLearning, reset, file };
}
