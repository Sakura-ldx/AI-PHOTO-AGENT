/**
 * Static + wiring check for the client half.
 *
 * Compiles `client.js`, runs its factory with a React stub, applies the module
 * against a fake Cordis context, and asserts the two slot registrations, locale
 * dictionaries and stylesheet. This is the only UI behavior verifiable without
 * a connected browser.
 *
 * Run: node scripts/check-client.mjs
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const source = fs.readFileSync(path.join(root, 'client.js'), 'utf8');

const failures = [];
const check = (condition, message) => {
  if (!condition) failures.push(message);
};

const React = {
  createElement: (type, props, ...children) => ({ type, props, children }),
  useState: (initial) => [initial, () => {}],
  useEffect: () => {},
  useMemo: (factory) => factory(),
  useCallback: (factory) => factory,
  useRef: (initial) => ({ current: initial }),
  Component: class Component {
    constructor(props) {
      this.props = props || {};
      this.state = {};
    }
  },
  Fragment: Symbol('Fragment'),
};

let definition;
const window = { __ModuleLoader__: { load: (value) => { definition = value; } } };
const appendedStyles = [];
const documentStub = {
  head: { appendChild: (tag) => appendedStyles.push(tag) },
  createElement: (name) => ({
    tagName: name,
    textContent: '',
    attributes: {},
    setAttribute(key, value) {
      this.attributes[key] = value;
    },
    remove() {
      this.removed = true;
    },
  }),
};
const requireModule = (name) => {
  if (name === 'react') return React;
  throw new Error(`unexpected runtime require("${name}")`);
};

try {
  new Function('window', 'require', 'document', source)(window, requireModule, documentStub);
} catch (error) {
  console.error(`client.js failed to compile: ${error.message}`);
  process.exit(1);
}

check(Boolean(definition), 'client.js did not call __ModuleLoader__.load');
check(definition && definition.id === '@local/dsh-photo-agent', 'client module id mismatch');

let module;
try {
  module = definition.factory(requireModule);
} catch (error) {
  console.error(`factory threw: ${error.message}`);
  process.exit(1);
}

const registrations = [];
const effects = [];
const fakeCtx = {
  effect(callback, label) {
    effects.push(label || 'effect');
    const dispose = callback();
    return typeof dispose === 'function' ? dispose : () => {};
  },
  get() {
    return undefined;
  },
  on() {
    return () => {};
  },
  locale: {
    register(namespace, dicts) {
      check(namespace === 'photo-agent', 'locale namespace mismatch');
      check(Boolean(dicts && dicts.zh && dicts.en), 'locale dictionaries missing zh/en');
      if (dicts && dicts.zh && dicts.en) {
        const zhKeys = Object.keys(dicts.zh).sort();
        const enKeys = Object.keys(dicts.en).sort();
        check(
          zhKeys.length === enKeys.length && zhKeys.every((key, index) => key === enKeys[index]),
          `locale key sets differ (zh=${zhKeys.length}, en=${enKeys.length})`,
        );
        const badEn = enKeys.filter((key) => typeof dicts.en[key] !== 'string' || !dicts.en[key].trim());
        check(badEn.length === 0, `en locale has non-string/empty values: ${badEn.join(', ')}`);
        const badZh = zhKeys.filter((key) => typeof dicts.zh[key] !== 'string' || !dicts.zh[key].trim());
        check(badZh.length === 0, `zh locale has non-string/empty values: ${badZh.join(', ')}`);
      }
      return () => {};
    },
    bind() {
      return (key) => key;
    },
    getSnapshot() {
      return { active: 'zh' };
    },
    subscribe() {
      return () => {};
    },
  },
  slots: {
    inject(key, callback) {
      check(typeof key === 'string' && key.length > 0, 'slot inject key missing');
      callback();
      return () => {};
    },
    register(descriptor, component) {
      registrations.push({ descriptor, component });
      return () => {};
    },
  },
};

try {
  module.apply(fakeCtx);
} catch (error) {
  console.error(`apply threw: ${error.message}`);
  process.exit(1);
}

check(Array.isArray(module.inject) && module.inject.includes('slots'), 'module must inject slots');
check(Array.isArray(module.inject) && module.inject.includes('locale'), 'module must inject locale');
check(
  appendedStyles.some(
    (tag) => typeof tag.textContent === 'string' && tag.textContent.includes('.photo-root'),
  ),
  'stylesheet content looks wrong',
);
check(
  appendedStyles.some(
    (tag) =>
      typeof tag.textContent === 'string' &&
      tag.textContent.includes('.photo-lightbox') &&
      tag.textContent.includes('.photo-lightbox-stage') &&
      tag.textContent.includes('.photo-lightbox-zoom'),
  ),
  'lightbox stylesheet missing',
);
check(source.includes("addEventListener('wheel'"), 'wheel listener missing');
check(source.includes('onPointerDown'), 'pointer drag handler missing');
check(source.includes("'zoom.reset'"), 'zoom reset label missing');

const sidebar = registrations.find(
  (entry) => entry.descriptor && entry.descriptor.name === 'sidebar.panellist',
);
const main = registrations.find((entry) => entry.descriptor && entry.descriptor.name === 'main');

check(Boolean(sidebar), 'missing sidebar.panellist registration');
check(Boolean(sidebar && sidebar.descriptor.id === 'photo-agent'), 'sidebar id must be photo-agent');
check(Boolean(sidebar && typeof sidebar.descriptor.label === 'function'), 'sidebar label must be a thunk');
check(Boolean(sidebar && typeof sidebar.component === 'function'), 'sidebar entry needs a component');
check(Boolean(main), 'missing main registration');
check(Boolean(main && main.descriptor.key === 'photo-agent'), 'main key must be photo-agent');
check(Boolean(main && typeof main.component === 'function'), 'main entry needs a component');
check(effects.some((label) => String(label).includes('dictionaries')), 'locale dictionaries effect missing');
check(effects.some((label) => String(label).includes('stylesheet')), 'stylesheet effect missing');

if (failures.length > 0) {
  console.error('client check failed:');
  for (const failure of failures) console.error(` - ${failure}`);
  process.exit(1);
}

console.log(`client.js OK: ${registrations.length} slot registrations, ${effects.length} effects`);
