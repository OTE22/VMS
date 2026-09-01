/*
 * XSS regression guard for the users table.
 *
 * esc() being correct is not enough - the row renderer has to actually USE it on
 * every field. This loads the real page script out of admin_users.html and
 * renders a row from a user record whose every string field is an attack, then
 * asserts nothing executable survives.
 *
 * Run: node --test tests/admin_users_render.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const UI = fs.readFileSync(path.join(REPO, 'InferenceNode/static/js/armyeye-ui.js'), 'utf8');
const HTML = fs.readFileSync(path.join(REPO, 'InferenceNode/templates/admin_users.html'), 'utf8');

/** Page behaviour now lives in an external file (no inline script in the template). */
const PAGE = fs.readFileSync(path.join(REPO, 'InferenceNode/static/js/admin-users.js'), 'utf8');

function load() {
    // admin-users.js initialises against the DOM on load; stub just enough of it.
    const noop = () => {};
    const el = {
        addEventListener: noop, removeAttribute: noop, setAttribute: noop,
        getAttribute: () => null, appendChild: noop, prepend: noop, focus: noop,
        reset: noop, remove: noop, closest: () => null,
        querySelectorAll: () => [], querySelector: () => null,
        classList: { toggle: noop, add: noop, remove: noop, contains: () => false },
        textContent: '', innerHTML: '', value: '', type: 'text', checked: false,
        dataset: {}, style: {}, hidden: false, disabled: false, colSpan: 0,
        tabIndex: 0, title: '', tagName: 'DIV',
    };
    const ctx = vm.createContext({
        console,
        document: {
            getElementById: () => el,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => Object.create(el),
            addEventListener: noop,
            body: el,
            readyState: 'complete',
        },
        debounce: (fn) => fn,
        showAlert: noop,
        fetchJSON: async () => { throw new Error('not used'); },
        bootstrap: { Modal: { getOrCreateInstance: () => ({ show: noop, hide: noop }) } },
    });
    // admin-users.js resolves its shared helpers through window[name], so the global
    // object must BE the context (that is how it behaves in a browser).
    ctx.window = ctx;
    vm.runInContext(UI + '\n' + PAGE, ctx);

    // The page script is an IIFE; it publishes a narrow surface for exactly this guard.
    const api = ctx.__adminUsersTestApi;
    assert.ok(api, 'admin-users.js did not expose its test surface');
    return { ...api, esc: ctx.esc };
}

const ATTACK = '<img src=x onerror=alert(1)>';

test('rowHtml escapes every user-controlled field', () => {
    const ctx = load();
    const html = ctx.rowHtml({
        id: 7,
        username: ATTACK,
        full_name: '"><script>alert("name")</script>',
        email: "' onfocus='alert(1)",
        role: '<b>admin</b>',
        is_active: true,
        must_change_password: true,
        last_login: '2026-01-02T03:04:05',
        created_at: '2026-01-01T00:00:00',
    });

    // What matters is that no payload produces a real TAG. (Escaped text still
    // contains the substring "onerror=" - that is inert, so asserting on it
    // would be testing the wrong thing.)
    assert.ok(!html.includes('<img'), 'username payload produced a real <img> tag');
    assert.ok(!html.includes('<script'), 'full_name payload produced a real <script> tag');
    assert.ok(!html.includes('<b>admin</b>'), 'role payload produced real markup');

    // Every tag in the output must be one the renderer itself wrote.
    const ALLOWED = new Set(['tr', 'td', 'div', 'span', 'ul', 'li', 'button', 'i', 'hr',
                             'code', 'h6']);   // h6 = kebab menu group labels
    for (const [, name] of html.matchAll(/<\/?([a-zA-Z][a-zA-Z0-9]*)/g)) {
        assert.ok(ALLOWED.has(name.toLowerCase()), `unexpected <${name}> injected into the row`);
    }

    // ...and the payloads are still rendered, escaped, rather than dropped.
    assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
    assert.ok(html.includes('&quot;&gt;&lt;script&gt;'));
    assert.ok(html.includes('&#39; onfocus=&#39;alert(1)'));
});

test('attribute contexts (title, aria-label) are escaped too', () => {
    const ctx = load();
    const html = ctx.rowHtml({
        id: 1, username: 'a" autofocus onfocus="alert(1)', full_name: null,
        email: null, role: 'user', is_active: false, must_change_password: false,
        last_login: null, created_at: null,
    });
    // aria-label="Actions for ${username}" is the risky one: a raw " inside the
    // value would close the attribute and let `autofocus onfocus=` become real.
    const labels = html.match(/aria-label="[^"]*"/g) || [];
    assert.ok(labels.length > 0, 'no aria-label rendered');
    const actions = labels.find(l => l.includes('Actions for'));
    assert.ok(actions, 'row action button lost its aria-label');
    assert.ok(actions.includes('&quot;'), 'the quote was not escaped');
    assert.ok(!/autofocus onfocus="/.test(html), 'attribute breakout produced a real handler');
});

test('null full_name/email render as an em dash, not "null"', () => {
    const ctx = load();
    const html = ctx.rowHtml({
        id: 1, username: 'joe', full_name: null, email: null, role: 'user',
        is_active: true, must_change_password: false, last_login: null, created_at: null,
    });
    assert.ok(!html.includes('>null<'), 'rendered the literal string null');
    assert.ok(html.includes('—'));
});

test('last active admin has destructive actions pre-disabled with the backend message', () => {
    const ctx = load();
    const onlyAdmin = { id: 1, username: 'root', full_name: null, email: null,
                        role: 'admin', is_active: true, must_change_password: false,
                        last_login: null, created_at: null };
    ctx.setUsers([onlyAdmin]);
    const html = ctx.rowHtml(onlyAdmin);

    // The exact strings the backend returns on 400, surfaced as tooltips.
    assert.ok(html.includes('Cannot remove admin role from the last active administrator'));
    assert.ok(html.includes('Cannot deactivate the last active administrator'));
    assert.ok(html.includes('Cannot delete the last active administrator'));
    // Disabled items must not carry a clickable action.
    assert.ok(!/data-action="delete"/.test(html), 'delete still actionable for last admin');
    assert.ok(!/data-action="role"/.test(html), 'role change still actionable for last admin');

    // With a second active admin present, the guards lift.
    ctx.setUsers([onlyAdmin, { ...onlyAdmin, id: 2, username: 'second' }]);
    const html2 = ctx.rowHtml(onlyAdmin);
    assert.ok(/data-action="delete"/.test(html2));
    assert.ok(/data-action="role"/.test(html2));
});

test('client-side filtering matches username, full name and email', () => {
    const ctx = load();
    ctx.setUsers([
        { id: 1, username: 'alice', full_name: 'Alice Smith', email: 'a@x.io', role: 'admin', is_active: true },
        { id: 2, username: 'bob', full_name: null, email: 'bob@y.io', role: 'user', is_active: false },
        { id: 3, username: 'carol', full_name: 'Carol Jones', email: null, role: 'user', is_active: true },
    ]);
    const f = { search: '', role: 'all', status: 'all' };
    const setF = (o) => ctx.setFilters(Object.assign(f, o));

    setF({ search: 'smith', role: 'all', status: 'all' });
    assert.equal(ctx.applyFilters().length, 1);          // full name

    setF({ search: 'y.io' });
    assert.equal(ctx.applyFilters().length, 1);          // email

    setF({ search: '', role: 'user' });
    assert.equal(ctx.applyFilters().length, 2);

    setF({ role: 'all', status: 'disabled' });
    assert.equal(ctx.applyFilters().length, 1);

    setF({ status: 'active', search: 'CAROL' });         // case-insensitive
    assert.equal(ctx.applyFilters().length, 1);

    // A null field must not throw when searched.
    setF({ search: 'zzz', status: 'all' });
    assert.equal(ctx.applyFilters().length, 0);
});
