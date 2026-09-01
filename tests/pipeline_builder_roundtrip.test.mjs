/*
 * Pipeline Builder serialization contract (Phase 2).
 *
 * Extracts the real collector functions out of pipeline_builder.html and runs them
 * against a fake DOM to prove the secret-handling contract the server relies on:
 *
 *   untouched sanitized secret field  -> "***" (sentinel)      -> server keeps stored secret
 *   blanked secret field              -> key OMITTED           -> server keeps stored secret
 *   "Clear stored credential" checked -> null                  -> server clears
 *   NEVER "" for a secret unless the operator explicitly cleared it
 *
 * plus the local-media canonical form: collector emits relative_source, drops source.
 *
 * Run: node --test tests/pipeline_builder_roundtrip.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const HTML = fs.readFileSync(path.join(REPO, 'InferenceNode/templates/pipeline_builder.html'), 'utf8').split(String.fromCharCode(13,10)).join(String.fromCharCode(10)).split(String.fromCharCode(13)).join('');

function extract(fnName) {
    const start = HTML.indexOf(`function ${fnName}(`);
    assert.ok(start !== -1, `${fnName} not found in pipeline_builder.html`);
    // function body ends at the first "\n}\n" after start
    const end = HTML.indexOf('\n}\n', start);
    return HTML.slice(start, end + 3);
}

/** Build a context with the collectors + a fake DOM populated from `fields`. */
function ctxWith(fields) {
    // fields: { id: {tagName, type, value, checked} }
    const doc = {
        getElementById: (id) => (Object.prototype.hasOwnProperty.call(fields, id)
            ? { id, tagName: 'INPUT', type: 'text', value: '', checked: false, ...fields[id] }
            : null),
    };
    const ctx = vm.createContext({ console, document: doc });
    // globals the collectors read
    vm.runInContext('var availableFrameSourceTypes = []; var availableDestinationTypes = [];', ctx);
    vm.runInContext(extract('isLocalMediaSourceType'), ctx);
    vm.runInContext(extract('collectFrameSourceConfigFromSchema'), ctx);
    vm.runInContext(extract('collectDestinationConfigFromSchema'), ctx);
    return ctx;
}

const IPCAM_SCHEMA = { type: 'ip_camera', config_schema: { fields: [
    { name: 'source', type: 'text', required: true, label: 'Camera URL' },
    { name: 'username', type: 'text', required: false },
    { name: 'password', type: 'password', required: false },
] } };
const MQTT_SCHEMA = { type: 'mqtt', config_schema: { fields: [
    { name: 'server', type: 'text', required: true },
    { name: 'password', type: 'password', required: false },
] } };
const VIDEO_SCHEMA = { type: 'video_file', config_schema: { fields: [
    { name: 'source', type: 'text', required: true, label: 'Video file' },
    { name: 'loop', type: 'checkbox' },
] } };

test('untouched sanitized secret serializes as the sentinel, never blank', () => {
    const ctx = ctxWith({
        source: { value: 'rtsp://***@10.0.0.1/stream1' }, username: { value: 'user' },
        password: { type: 'password', value: '***' }, password__clear: { type: 'checkbox', checked: false },
    });
    vm.runInContext('availableFrameSourceTypes = [' + JSON.stringify(IPCAM_SCHEMA) + ']', ctx);
    const r = vm.runInContext("collectFrameSourceConfigFromSchema('ip_camera')", ctx);
    assert.equal(r.config.password, '***');
    assert.notEqual(r.config.password, '');
});

test('blanked secret field is OMITTED (server preserves), not sent as ""', () => {
    const ctx = ctxWith({
        source: { value: 'rtsp://***@10.0.0.1/stream1' },
        password: { type: 'password', value: '   ' }, password__clear: { type: 'checkbox', checked: false },
    });
    vm.runInContext('availableFrameSourceTypes = [' + JSON.stringify(IPCAM_SCHEMA) + ']', ctx);
    const r = vm.runInContext("collectFrameSourceConfigFromSchema('ip_camera')", ctx);
    assert.ok(!('password' in r.config), 'blank secret must be omitted, got ' + JSON.stringify(r.config));
});

test('only the explicit Clear affordance sends null', () => {
    const ctx = ctxWith({
        source: { value: 'rtsp://***@10.0.0.1/stream1' },
        password: { type: 'password', value: '***' }, password__clear: { type: 'checkbox', checked: true },
    });
    vm.runInContext('availableFrameSourceTypes = [' + JSON.stringify(IPCAM_SCHEMA) + ']', ctx);
    const r = vm.runInContext("collectFrameSourceConfigFromSchema('ip_camera')", ctx);
    assert.equal(r.config.password, null);
});

test('editing an unrelated field on a sanitized pipeline cannot clear a credential', () => {
    // operator changed username only; password input still holds the sentinel
    const ctx = ctxWith({
        source: { value: 'rtsp://***@10.0.0.1/stream1' }, username: { value: 'renamed' },
        password: { type: 'password', value: '***' }, password__clear: { type: 'checkbox', checked: false },
    });
    vm.runInContext('availableFrameSourceTypes = [' + JSON.stringify(IPCAM_SCHEMA) + ']', ctx);
    const r = vm.runInContext("collectFrameSourceConfigFromSchema('ip_camera')", ctx);
    assert.equal(r.config.username, 'renamed');
    assert.ok(r.config.password === '***' || !('password' in r.config));
    assert.notEqual(r.config.password, '');
    assert.notEqual(r.config.password, null);
});

test('destination collector reads dest_-prefixed ids (no collision with frame-source fields)', () => {
    const ctx = ctxWith({
        // frame-source side holds the CAMERA password under the bare id
        password: { type: 'password', value: 'CAMERA-SECRET' },
        // destination side under its own prefixed ids
        dest_server: { value: 'broker' },
        dest_password: { type: 'password', value: '***' }, dest_password__clear: { type: 'checkbox', checked: false },
    });
    vm.runInContext('availableDestinationTypes = [' + JSON.stringify(MQTT_SCHEMA) + ']', ctx);
    const r = vm.runInContext("collectDestinationConfigFromSchema('mqtt')", ctx);
    assert.equal(r.config.server, 'broker');
    assert.equal(r.config.password, '***', 'must read dest_password, never the camera password');
});

test('local media persists ONE canonical form: relative_source, never source', () => {
    const ctx = ctxWith({ source: { tagName: 'SELECT', value: 'clips/cam1.mp4' }, loop: { type: 'checkbox', checked: true } });
    vm.runInContext('availableFrameSourceTypes = [' + JSON.stringify(VIDEO_SCHEMA) + ']', ctx);
    const r = vm.runInContext("collectFrameSourceConfigFromSchema('video_file')", ctx);
    assert.equal(r.config.relative_source, 'clips/cam1.mp4');
    assert.ok(!('source' in r.config));
    assert.equal(r.isValid, true);
});

test('missing-schema early return has the full shape the submit handler destructures', () => {
    const ctx = ctxWith({});
    const r = vm.runInContext("collectFrameSourceConfigFromSchema('unknown_type')", ctx);
    assert.deepEqual(Object.keys(r).sort(), ['config', 'isValid', 'missingFields', 'requiredFields']);
    assert.equal(r.isValid, true);
});

test('populateFrameSourceConfig hands local media to the picker via pendingSource (relative wins, legacy read-only)', () => {
    const src = extract('populateFrameSourceConfig');
    assert.ok(src.includes('config.relative_source'), 'must read the canonical relative_source');
    assert.ok(src.includes('legacyAbsolute'), 'legacy absolute source is a compatibility READ');
    assert.ok(src.includes('loadMediaSources('), 'delegates pre-selection to the picker loader');
    const loader = extract('loadMediaSources');
    assert.ok(loader.includes('matches.length === 1'), 'legacy basename resolves only when UNIQUE');
    assert.ok(loader.includes('matches.length === 0'), '0 matches -> nothing selected + warning');
});

test('duplicate paths use the server-authoritative endpoint on both pages', () => {
    const mgmt = fs.readFileSync(path.join(REPO, 'InferenceNode/templates/pipeline_management.html'), 'utf8').split(String.fromCharCode(13,10)).join(String.fromCharCode(10)).split(String.fromCharCode(13)).join('');
    for (const [name, html] of [['builder', HTML], ['management', mgmt]]) {
        assert.ok(html.includes('/duplicate`'), `${name}: must call POST /api/pipeline/<id>/duplicate`);
        assert.ok(!html.includes('function generateDuplicateName'), `${name}: client-side name generation removed`);
    }
    // management delete prunes the authoritative cache too, and the modal Edit passes an id
    assert.ok(mgmt.includes('allPipelines = allPipelines.filter(p => p.id !== pipelineId)'));
    assert.ok(mgmt.includes('onclick="editPipeline(currentDetailPipelineId)"'));
    assert.ok(!mgmt.includes('onclick="editPipeline()"'));
});

test('builder edit path awaits loaders instead of timers', () => {
    assert.ok(HTML.includes('window.__loadersReady'));
    assert.ok(!/setTimeout\(\(\) => \{\s*populateFrameSourceConfig/.test(HTML), 'no 100ms hydration timer');
    assert.ok(!/setTimeout\(\(\) => \{\s*editPipeline\(editPipelineId\)/.test(HTML), 'no 500ms edit timer');
});
