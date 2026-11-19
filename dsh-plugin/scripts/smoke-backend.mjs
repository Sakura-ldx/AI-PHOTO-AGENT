/**
 * End-to-end smoke test for the Host service against the real Python backend.
 *
 * It starts the project's FastAPI app on a free port with the mock analysis
 * provider, a temporary data root and a temporary Lightroom root, then exercises
 * upload, analysis, preview, XMP, develop settings, the Lightroom queue and the
 * learning workflow. No photos leave the machine and no real Lightroom catalog
 * is touched.
 *
 * Run: node scripts/smoke-backend.mjs
 */

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createBackend } from '../src/backend.js';
import { loadConfig } from '../src/config.js';
import { createPhotoService } from '../src/service.js';
import { createStateStore } from '../src/store.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const failures = [];
const checks = [];

function check(condition, message) {
  if (condition) {
    checks.push(message);
  } else {
    failures.push(message);
  }
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = address && typeof address === 'object' ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

function pythonPath() {
  const candidate =
    process.platform === 'win32'
      ? path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
      : path.join(repoRoot, '.venv', 'bin', 'python');
  return fs.existsSync(candidate) ? candidate : 'python';
}

function createFixture(python, target) {
  const code = [
    'import sys',
    'from PIL import Image',
    'image = Image.new("RGB", (320, 240))',
    'pixels = image.load()',
    'for y in range(240):',
    '    for x in range(320):',
    '        pixels[x, y] = (x % 256, y % 256, (x + y) % 256)',
    'image.save(sys.argv[1], "JPEG", quality=90)',
  ].join('\n');
  const result = spawnSync(python, ['-c', code, target], {
    encoding: 'utf8',
    timeout: 60_000,
    cwd: repoRoot,
  });
  if (result.status !== 0) {
    throw new Error(`生成测试图片失败：${result.stderr || result.stdout || result.status}`);
  }
}

function listFiles(directory, depth = 0) {
  if (depth > 3) return [];
  let entries;
  try {
    entries = fs.readdirSync(directory, { withFileTypes: true });
  } catch {
    return [];
  }
  const files = [];
  for (const entry of entries) {
    const child = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...listFiles(child, depth + 1));
    else files.push(child);
  }
  return files;
}

async function main() {
  const python = pythonPath();
  if (python.includes(path.sep) && !fs.existsSync(python)) {
    throw new Error(`找不到虚拟环境 Python：${python}`);
  }

  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-photo-agent-smoke-'));
  const dataRoot = path.join(tempRoot, 'data-root');
  const lightroomRoot = path.join(tempRoot, 'lightroom');
  const fixture = path.join(tempRoot, 'fixture.jpg');
  fs.mkdirSync(dataRoot, { recursive: true });
  fs.mkdirSync(lightroomRoot, { recursive: true });
  createFixture(python, fixture);

  const port = await freePort();
  const env = { ...process.env };
  delete env.DSH_PHOTO_AGENT_BASE_URL;
  Object.assign(env, {
    DSH_PHOTO_AGENT_APP_DIR: repoRoot,
    DSH_PHOTO_AGENT_PYTHON: python,
    DSH_PHOTO_AGENT_PORT: String(port),
    DSH_PHOTO_AGENT_PROVIDER: 'mock',
    DSH_PHOTO_AGENT_DATA_DIR: dataRoot,
    DSH_PHOTO_AGENT_LIGHTROOM_ROOT: lightroomRoot,
    DSH_PHOTO_AGENT_START_TIMEOUT_MS: '60000',
    DSH_PHOTO_AGENT_ANALYSIS_TIMEOUT_MS: '60000',
  });
  const config = loadConfig(env);
  const store = createStateStore({ file: config.stateFile });
  const backend = createBackend(config, { logger: console });
  const service = createPhotoService({ config, backend, store, logger: console });

  let photoId = null;
  let styleId = null;
  let learningId = null;

  try {
    const status = await service.ensureReady();
    check(status.ready === true && status.managed === true, 'backend spawned and ready');

    const data = fs.readFileSync(fixture);
    const uploaded = await service.uploadImage({
      data,
      filename: 'fixture.jpg',
      contentType: 'image/jpeg',
    });
    photoId = uploaded.photo_id;
    check(/^[0-9a-f]{32}$/.test(photoId), 'upload returned a 32-hex photo id');
    check(uploaded.metadata?.width === 320 && uploaded.metadata?.height === 240, 'upload metadata dimensions');
    check(uploaded.metadata?.format === 'JPEG', 'upload metadata format');

    const display = await service.fetchAsset(`/api/photos/${photoId}/display`);
    check(display.buffer.length > 100 && display.buffer[0] === 0xff && display.buffer[1] === 0xd8, 'display asset is a JPEG');

    const analysis = await service.analyze(photoId, 'mock');
    check(Array.isArray(analysis.recommended_styles) && analysis.recommended_styles.length === 3, 'mock analysis returns three styles');
    check(typeof analysis.overall_summary === 'string' && analysis.overall_summary.length > 0, 'mock analysis has a summary');
    styleId = analysis.recommended_styles[0].id;

    const preview = await service.preview(photoId, styleId, 0.7);
    check(Boolean(preview.preview_path && fs.existsSync(preview.preview_path)), 'preview file exists');
    const previewBytes = fs.readFileSync(preview.preview_path);
    check(previewBytes[0] === 0xff && previewBytes[1] === 0xd8, 'preview file is a JPEG');
    const proxiedPreview = await service.fetchAsset(preview.preview_url);
    check(proxiedPreview.buffer.length === previewBytes.length, 'preview proxy byte count');

    const knowledgeSummary = await service.knowledgeGet(photoId);
    check(knowledgeSummary && knowledgeSummary.photo_id === photoId, 'knowledge summary exists after analysis');
    check(
      Array.isArray(knowledgeSummary.directions) && knowledgeSummary.directions.length === 3,
      'knowledge summary has three editing directions',
    );
    check(
      knowledgeSummary.directions.every(
        (direction) => direction.recommended_intensity >= 0.4 && direction.recommended_intensity <= 1,
      ),
      'knowledge directions have recommended intensity in range',
    );
    check(knowledgeSummary.directions.some((direction) => Boolean(direction.preview_url)), 'knowledge directions have previews');
    const knowledgePreview = await service.fetchAsset(
      knowledgeSummary.directions.find((direction) => direction.preview_url).preview_url,
    );
    check(knowledgePreview.buffer.length > 100, 'knowledge preview image proxied');

    const knowledgeOverview = await service.knowledgeOverview();
    check(knowledgeOverview.total_summaries >= 1, 'knowledge overview counts summaries');
    const knowledgeList = await service.knowledgeList({ limit: 10 });
    check(
      Array.isArray(knowledgeList.items) && knowledgeList.items.some((item) => item.photo_id === photoId),
      'knowledge list contains the analyzed photo',
    );
    const filteredList = await service.knowledgeList({ limit: 10, dimension: 'light', tag: '逆光' });
    check(typeof filteredList.total === 'number', 'knowledge list accepts tag filters');
    const rebuiltSummary = await service.knowledgeRebuild(photoId);
    check(rebuiltSummary && rebuiltSummary.directions.length === 3, 'knowledge rebuild returns a summary');

    const exported = await service.exportPreset(photoId, styleId, 0.7);
    check(Boolean(exported.preset_path && fs.existsSync(exported.preset_path)), 'XMP file exists');
    check(fs.readFileSync(exported.preset_path, 'utf8').includes('<x:xmpmeta'), 'XMP contains x:xmpmeta');
    check(exported.mapping_version === 'lightroom_xmp_v2', 'XMP mapping version');

    const develop = await service.developSettings(photoId, styleId, 0.7);
    check(Boolean(develop.develop_settings && Object.keys(develop.develop_settings).length > 0), 'develop settings non-empty');
    check(develop.parameters?.schema_version === 'editing_parameters_v1', 'develop parameters schema');

    const lightroom = await service.lightroomStatus();
    check(lightroom.detected === true, 'temporary Lightroom root detected');
    check(lightroom.plugin_source.endsWith('AIPhotoAgent.lrplugin'), 'Lightroom plugin source identity');

    const installed = await service.installLightroomPlugin();
    check(installed.installed === true, 'Lightroom plugin installed into temporary root');
    check(
      fs.existsSync(path.join(lightroomRoot, 'Modules', 'AIPhotoAgent.lrplugin', 'Info.lua')),
      'installed Info.lua exists',
    );

    const sent = await service.sendToLightroom({
      photoId,
      styleId,
      intensity: 0.7,
      installPlugin: false,
    });
    check(sent.status === 'queued', 'Lightroom send queued');
    check(Boolean(sent.job_id), 'Lightroom job id present');
    const inboxFile = path.join(lightroomRoot, 'AI Photo Agent Bridge', 'inbox', `${sent.job_id}.json`);
    check(fs.existsSync(inboxFile), 'Lightroom queue JSON exists');
    const job = JSON.parse(fs.readFileSync(inboxFile, 'utf8'));
    check(job.develop_settings_version === 'lightroom_develop_v1', 'queue job schema version');
    const presetDir = path.join(lightroomRoot, 'Develop Presets', 'AI Photo Agent');
    check(listFiles(presetDir).some((file) => file.endsWith('.xmp')), 'queued preset copied to Lightroom folder');

    const created = await service.learningCreate('街头摄影');
    learningId = created.id;
    check(/^[0-9a-f]{32}$/.test(learningId), 'learning session created');
    await service.learningAddReference({
      sessionId: learningId,
      data,
      filename: 'fixture.jpg',
      contentType: 'image/jpeg',
      sourceUrl: 'https://example.com/photo',
      author: 'smoke test',
      license: 'test fixture',
    });
    const analyzed = await service.learningAnalyze(learningId);
    check(analyzed.references.length === 1, 'learning session has one reference');
    check(Boolean(analyzed.summary && analyzed.summary.style_overview), 'learning summary present');
    const fetched = await service.learningGet(learningId);
    check(fetched.id === learningId && fetched.references[0].analysis?.practical_lessons?.length > 0, 'learning get returns analysis');
    const learningImage = await service.fetchAsset(fetched.references[0].image_url);
    check(learningImage.buffer.length > 100, 'learning reference image proxied');

    const cached = store.read();
    check(cached?.photo_id === photoId && cached?.analysis?.recommended_styles?.length === 3, 'state cache has last analysis');
    check(cached?.learning_session_id === learningId, 'state cache has last learning session');

    const state = service.describe();
    check(state.backend.ready === true && state.config.port === port, 'describe reports runtime state');
  } finally {
    try {
      await service.dispose();
    } catch (error) {
      failures.push(`dispose failed: ${error.message}`);
    }
    if (failures.length > 0) {
      const logs = backend.logTail(40);
      if (logs) console.error(`\n--- backend log tail ---\n${logs}\n`);
    }
    try {
      fs.rmSync(tempRoot, { recursive: true, force: true });
    } catch {
      /* temp cleanup is best effort */
    }
  }

  if (failures.length > 0) {
    console.error('smoke-backend failed:');
    for (const failure of failures) console.error(` - ${failure}`);
    process.exit(1);
  }
  console.log(`smoke-backend OK: ${checks.length} checks`);
  for (const message of checks) console.log(` - ${message}`);
}

main().catch((error) => {
  console.error(`smoke-backend crashed: ${error.stack || error.message}`);
  process.exit(1);
});
