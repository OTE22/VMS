import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const root = new URL('../', import.meta.url);
const read = path => fs.readFileSync(new URL(path, root), 'utf8');
const inline = page => [...read(`InferenceNode/templates/${page}.html`).matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

function pageContext(page, response) {
    const elements = new Map();
    const alerts = [];
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            innerHTML: '', textContent: '', value: '', checked: false,
            addEventListener(event, fn) { this[event] = fn; },
        });
        return elements.get(id);
    };
    const ctx = vm.createContext({
        document: { getElementById: element, addEventListener() {}, querySelectorAll: () => [] },
        window: { location: { origin: 'http://localhost' }, addEventListener() {} },
        fetchJSON: async () => response,
        fetch: async () => response,
        showAlert: (...args) => alerts.push(args),
        bootstrap: { Modal: class { show() {} static getInstance() { return { hide() {} }; } } },
        console: { log() {}, warn() {}, error() {} },
    });
    // Exercise the real shared parsing helper, rather than mocking its return shape.
    vm.runInContext(read('InferenceNode/static/js/armyeye-ui.js'), ctx);
    vm.runInContext(inline(page), ctx);
    return { ctx, element, alerts };
}

const response = (status, body) => ({
    ok: status >= 200 && status < 300, status,
    json: async () => body, text: async () => JSON.stringify(body),
});
const media = { media_id: 'clip-id', relative_path: 'clip.mp4', status: 'AVAILABLE',
    validation_status: 'PASSED', size_bytes: 1234, references: [] };

test('media listing reads JSON from Response and displays saved records', async () => {
    const { ctx, element } = pageContext('media', response(200, { media: [media], count: 1, total_size_bytes: 1234 }));
    await ctx.loadMedia();
    assert.match(element('mediaList').innerHTML, /clip\.mp4/);
    assert.match(element('mediaSummary').textContent, /1 file/);
});

test('media listing does not present a failed request as an empty library', async () => {
    const { ctx, element } = pageContext('media', response(503, { error: 'Database unavailable' }));
    await ctx.loadMedia();
    assert.match(element('mediaList').innerHTML, /Database unavailable/);
    assert.doesNotMatch(element('mediaList').innerHTML, /No media registered/);
});

for (const [status, body, expected] of [
    [200, { relative_path: 'clip.mp4' }, /Deleted clip\.mp4/],
    [409, { pipelines: [{ name: 'Lobby camera' }] }, /Still in use by: Lobby camera/],
    [500, { error: 'Delete transaction failed' }, /Delete transaction failed/],
]) {
    test(`media deletion handles parsed apiCall result (${status})`, async () => {
        const { ctx, element, alerts } = pageContext('media', response(status, body));
        vm.runInContext(`mediaCache = [${JSON.stringify(media)}];`, ctx);
        ctx.askDeleteMedia('clip-id');
        await element('confirmDeleteMedia').click();
        assert.match(alerts[0][1], expected);
        assert.doesNotMatch(alerts[0][1], /json is not a function/);
        assert.equal(element('confirmDeleteMedia').disabled, false);
    });
}

test('logs report request failure instead of manufacturing sample events', async () => {
    const { ctx, element } = pageContext('logs', response(503, { error: 'Logs unavailable' }));
    await ctx.loadLogs();
    assert.match(element('logContainer').innerHTML, /Logs unavailable/);
    assert.equal(vm.runInContext('allLogs.length', ctx), 0);
});

test('log settings hydrate from the GET response including a false checkbox', async () => {
    const { ctx, element } = pageContext('logs', response(200, {
        success: true, settings: { log_level: 'ERROR', max_log_size_mb: 42, retention_days: 9, file_logging_enabled: false },
    }));
    await ctx.loadLogSettings();
    assert.equal(element('globalLogLevel').value, 'ERROR');
    assert.equal(element('maxLogSize').value, 42);
    assert.equal(element('logRetention').value, 9);
    assert.equal(element('enableFileLogging').checked, false);
});

test('log messages and details are rendered as text, not HTML', () => {
    const { ctx, element } = pageContext('logs', response(200, {}));
    vm.runInContext(`filteredLogs = [{timestamp: '', level: 'INFO', component: '<img>', message: '<img onerror=alert(1)>', details: {text: '<script>'}}]; displayLogs();`, ctx);
    const html = element('logContainer').innerHTML;
    assert.doesNotMatch(html, /<img|<script>/);
    assert.match(html, /&lt;img/);
});

test('API explorer renders response payloads as escaped text', async () => {
    const { ctx, element } = pageContext('api_docs', response(200, { name: '<img onerror=alert(1)>' }));
    await ctx.testEndpoint('/api/models');
    assert.doesNotMatch(element('testResults').innerHTML, /<img/);
    assert.match(element('testResults').innerHTML, /&lt;img/);
});

// Check each template script's JavaScript syntax. Jinja directives are removed;
// this is syntax coverage, not a substitute for a rendered-browser smoke test.
for (const name of fs.readdirSync(new URL('InferenceNode/templates/', root)).filter(n => n.endsWith('.html'))) {
    test(`${name}: inline JavaScript parses`, () => {
        const source = inline(name.slice(0, -5)).replace(/\{%[\s\S]*?%\}/g, '').replace(/\{\{[\s\S]*?\}\}/g, 'null');
        new vm.Script(source, { filename: name });
    });
}

for (const enabled of [false, true]) {
    test(`telemetry charts keep polling after saving publishing enabled=${enabled}`, async () => {
        const { ctx, element } = pageContext('telemetry', response(200, { status: 'configured' }));
        let starts = 0;
        ctx.startTelemetryUpdates = () => { starts++; };
        element('telemetryEnabled').checked = enabled;
        element('publishInterval').value = '30';
        element('mqttPort').value = '1883';
        await element('telemetryConfigForm').submit({ preventDefault() {} });
        assert.equal(starts, 1);
    });
}
