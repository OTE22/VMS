/*
 * Tests for the shared admin UI helpers (static/js/armyeye-ui.js).
 *
 * Run directly:  node --test tests/armyeye_ui.test.mjs
 * Also invoked from tests/test_frontend_js.py so it runs with the normal suite.
 *
 * armyeye-ui.js is a plain <script> (no module system), so it is evaluated in a
 * vm context with only the globals it actually needs at call time.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SRC = fs.readFileSync(
    path.join(REPO, 'InferenceNode', 'static', 'js', 'armyeye-ui.js'), 'utf8');

/** Fresh sandbox with a scripted fetchJSON stub. */
function load(fetchJSON) {
    const ctx = vm.createContext({ fetchJSON, console, crypto });
    vm.runInContext(SRC, ctx);
    return ctx;
}

/** Minimal Response stand-in: apiCall only uses ok/status/text(). */
const resp = (status, body) => ({
    ok: status >= 200 && status < 300,
    status,
    text: async () => body,
});

/* ------------------------------------------------------------------ esc() */

test('esc escapes every character that can break out of HTML', () => {
    const { esc } = load();
    assert.equal(esc('&'), '&amp;');
    assert.equal(esc('<'), '&lt;');
    assert.equal(esc('>'), '&gt;');
    assert.equal(esc('"'), '&quot;');
    assert.equal(esc("'"), '&#39;');
});

test('esc neutralises script injection in a username', () => {
    const { esc } = load();
    const out = esc('<script>alert(1)</script>');
    assert.ok(!out.includes('<script'));
    assert.equal(out, '&lt;script&gt;alert(1)&lt;/script&gt;');
});

test('esc neutralises attribute-breakout payloads', () => {
    const { esc } = load();
    // The users table interpolates into title="..." and aria-label="..."
    const out = esc('" onmouseover="steal()" x="');
    assert.ok(!out.includes('"'));
    assert.ok(out.includes('&quot;'));
});

test('esc escapes ampersands before entities, not after', () => {
    const { esc } = load();
    // Naive ordering would turn < into &lt; and then & into &amp;lt;
    assert.equal(esc('&lt;'), '&amp;lt;');
});

test('esc renders null and undefined as empty, not as text', () => {
    const { esc } = load();
    assert.equal(esc(null), '');
    assert.equal(esc(undefined), '');
    assert.equal(esc(0), '0');
    assert.equal(esc(false), 'false');
});

/* --------------------------------------------------------------- fmtUtc() */

test('naive backend timestamps are read as UTC, not local time', () => {
    const { fmtUtc, isoTitle } = load();
    // The auth tables store naive UTC and isoformat() emits no Z.
    assert.equal(isoTitle('2026-01-02T03:04:05'), '2026-01-02T03:04:05.000Z');
    assert.equal(isoTitle('2026-01-02T03:04:05Z'), '2026-01-02T03:04:05.000Z');
    assert.equal(isoTitle('2026-01-02T03:04:05+00:00'), '2026-01-02T03:04:05.000Z');
    assert.equal(fmtUtc(null), '—');
    assert.equal(fmtUtc(''), '—');
    assert.equal(fmtUtc('not a date'), '—');
});

/* -------------------------------------------------------------- apiCall() */

test('apiCall returns parsed data on success', async () => {
    const { apiCall } = load(async () => resp(200, JSON.stringify({ users: [1, 2] })));
    const r = await apiCall('/api/users');
    assert.equal(r.ok, true);
    // JSON.parse ran inside the vm realm, so compare by value, not by prototype.
    assert.equal(JSON.stringify(r.data.users), '[1,2]');
    assert.equal(r.error, null);
});

test('apiCall surfaces the server error message verbatim', async () => {
    const { apiCall } = load(async () =>
        resp(400, JSON.stringify({ error: 'Cannot deactivate the last active administrator' })));
    const r = await apiCall('/api/users/1/active', { method: 'PUT' });
    assert.equal(r.ok, false);
    assert.equal(r.error, 'Cannot deactivate the last active administrator');
});

test('apiCall survives a non-JSON body (Flask HTML 400 from a CSRF abort)', async () => {
    const html = '<!doctype html><title>400 Bad Request</title><h1>Bad Request</h1>';
    const { apiCall } = load(async () => resp(400, html));
    const r = await apiCall('/api/users/1', { method: 'PATCH' });
    assert.equal(r.ok, false);
    assert.equal(r.status, 400);
    assert.equal(r.data, null);                       // JSON.parse threw, handled
    assert.match(r.error, /session token/i);          // human sentence, not "400"
    assert.ok(r.detail.includes('Bad Request'));      // raw body kept for debugging
});

test('apiCall maps bare statuses to human sentences', async () => {
    for (const [status, re] of [[403, /permission/i], [404, /not found/i],
                                [429, /too many/i], [500, /could not complete/i]]) {
        const { apiCall } = load(async () => resp(status, ''));
        const r = await apiCall('/x');
        assert.equal(r.ok, false);
        assert.match(r.error, re, `status ${status}`);
        assert.ok(!/^Request failed/.test(r.error), `status ${status} fell through`);
    }
});

test('apiCall reports a network failure instead of throwing', async () => {
    const { apiCall } = load(async () => { throw new TypeError('Failed to fetch'); });
    const r = await apiCall('/api/users');
    assert.equal(r.ok, false);
    assert.equal(r.status, 0);
    assert.match(r.error, /could not reach the server/i);
});

test('apiCall re-throws the auth redirect so fetchJSON can navigate', async () => {
    const { apiCall } = load(async () => { throw new Error('Not authenticated'); });
    await assert.rejects(() => apiCall('/api/users'), /Not authenticated/);
});

test('apiCall treats HTTP 200 with valid:false as a success (caller branches on body)', async () => {
    // /api/inference/engines/validate ALWAYS answers 200; the verdict is in the body.
    const { apiCall } = load(async () =>
        resp(200, JSON.stringify({ valid: false, error: 'Missing required methods: draw' })));
    const r = await apiCall('/api/inference/engines/validate', { method: 'POST' });
    assert.equal(r.ok, true);
    assert.equal(r.data.valid, false);
    assert.equal(r.data.error, 'Missing required methods: draw');
});

test('apiCall tolerates an empty body on 200 (e.g. 204-style replies)', async () => {
    const { apiCall } = load(async () => resp(200, ''));
    const r = await apiCall('/x');
    assert.equal(r.ok, true);
    assert.equal(r.data, null);
});

/* ------------------------------------------------------- suggestPassword() */

test('suggestPassword meets the backend minimum and has no ambiguous glyphs', () => {
    const { suggestPassword } = load();
    const pw = suggestPassword(16);
    assert.equal(pw.length, 16);
    assert.ok(pw.length >= 8);                 // service.hash_password rejects < 8
    assert.ok(!/[lIO01]/.test(pw));            // excluded to avoid transcription errors
});
