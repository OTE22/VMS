/*
 * Users & Access administration.
 *
 * Externalised from admin_users.html for maintainability and so the page carries no
 * inline handlers (a future CSP needs no 'unsafe-inline' for it). Note: CSP was NOT the
 * cause of the modal defect - that was a stacking context on .container-fluid.
 *
 * Every shared helper comes from armyeye-ui.js / app.js and is re-implemented nowhere:
 *   armyeye-ui.js : esc apiCall confirmDialog setButtonLoading attachPasswordToggle
 *                   suggestPassword skeletonRows stateRow fmtUtc isoTitle
 *   app.js        : fetchJSON showAlert debounce
 *
 * Backend truths this page is built on (auth/admin_routes.py):
 *   - no server-side search/sort/pagination -> filtering is client-side over the full list
 *   - role / active / reset-password bump permissions_version, signing the target out;
 *     profile edits do NOT
 *   - the last ACTIVE ADMIN cannot be demoted, disabled or deleted (400) -> pre-disabled
 *     here using the backend's own wording
 *   - nothing stops an admin acting on their own account -> warn explicitly
 *   - a CSRF failure returns an HTML 400 body -> apiCall() tolerates non-JSON
 */
'use strict';

(function () {

const COLS = 7;

const LAST_ADMIN_MSG = {
    role:   'Cannot remove admin role from the last active administrator',
    active: 'Cannot deactivate the last active administrator',
    del:    'Cannot delete the last active administrator',
};

const PERMS = [
    { key: 'can_view',  label: 'View'  },
    { key: 'can_start', label: 'Start' },
    { key: 'can_stop',  label: 'Stop'  },
    { key: 'can_edit',  label: 'Edit'  },
];

const ROLES = [
    { id: 'user',  title: 'Standard User', icon: 'fa-user',
      desc: 'Pipeline and stream access according to explicit assignments.' },
    { id: 'admin', title: 'Administrator', icon: 'fa-user-shield', elevated: true,
      desc: 'Full administration: users, models, engines and every pipeline.' },
];

/* Helpers this page cannot function without. Checked once, loudly. */
const REQUIRED_HELPERS = ['esc', 'apiCall', 'confirmDialog', 'setButtonLoading',
                          'attachPasswordToggle', 'suggestPassword', 'skeletonRows',
                          'stateRow', 'fmtUtc', 'isoTitle', 'showAlert', 'debounce'];

let allUsers = [];
let filters = { search: '', role: 'all', status: 'all' };
let lastRenderKey = '';
let currentUserId = null;
let modals = {};
let dom = {};

/* ===================================================================== init */

function fatal(message, detail) {
    console.error('[users] ' + message, detail || '');
    const host = document.getElementById('users-tbody') || document.body;
    const box = document.createElement('div');
    box.className = 'alert alert-danger m-3';
    // textContent: never interpolate a failure string into innerHTML.
    box.textContent = 'Users Management could not start: ' + message;
    if (host.tagName === 'TBODY') {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = COLS;
        td.appendChild(box);
        tr.appendChild(td);
        host.innerHTML = '';
        host.appendChild(tr);
    } else {
        host.prepend(box);
    }
}

function cacheDom() {
    const ids = ['users-tbody', 'result-count', 'user-search', 'btn-clear-search',
                 'filter-role', 'filter-status', 'btn-refresh', 'btn-add-user',
                 'stat-total', 'stat-active', 'stat-admins', 'stat-disabled',
                 'addUserModal', 'editUserModal', 'resetPwModal', 'accessModal',
                 'add-user-form', 'edit-user-form', 'reset-pw-form'];
    const missing = [];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (!el) missing.push(id);
        dom[id] = el;
    });
    return missing;
}

function init() {
    const missingHelpers = REQUIRED_HELPERS.filter(n => typeof window[n] !== 'function');
    if (missingHelpers.length) {
        return fatal('required helpers are unavailable (' + missingHelpers.join(', ') +
                     '). Check that armyeye-ui.js and app.js loaded.');
    }
    if (!window.bootstrap || !window.bootstrap.Modal) {
        return fatal('Bootstrap JavaScript did not load, so dialogs cannot open.');
    }
    const missingNodes = cacheDom();
    if (missingNodes.length) {
        return fatal('page markup is missing required elements: ' + missingNodes.join(', '));
    }

    modals = {
        add:    bootstrap.Modal.getOrCreateInstance(dom['addUserModal']),
        edit:   bootstrap.Modal.getOrCreateInstance(dom['editUserModal']),
        reset:  bootstrap.Modal.getOrCreateInstance(dom['resetPwModal']),
        access: bootstrap.Modal.getOrCreateInstance(dom['accessModal']),
    };

    const meta = document.querySelector('meta[name="current-user-id"]');
    currentUserId = meta ? (parseInt(meta.content, 10) || null) : null;

    bindEvents();
    loadUsers();
}

/* =============================================================== rendering */

function initials(u) {
    const src = (u.full_name || u.username || '?').trim();
    const parts = src.split(/\s+/).filter(Boolean);
    return parts.length > 1 ? parts[0][0] + parts[1][0] : src.slice(0, 2);
}

function activeAdminCount() {
    return allUsers.filter(u => u.role === 'admin' && u.is_active).length;
}

function isLastActiveAdmin(u) {
    return u.role === 'admin' && u.is_active && activeAdminCount() === 1;
}

function applyFilters() {
    const q = filters.search.toLowerCase();
    return allUsers.filter(u => {
        if (filters.role !== 'all' && u.role !== filters.role) return false;
        if (filters.status === 'active' && !u.is_active) return false;
        if (filters.status === 'disabled' && u.is_active) return false;
        if (filters.status === 'mustchange' && !u.must_change_password) return false;
        if (!q) return true;
        return [u.username, u.full_name, u.email]
            .some(v => v && String(v).toLowerCase().includes(q));
    });
}

function menuItem({ action, id, icon, label, danger, disabledReason }) {
    const cls = 'dropdown-item' + (danger ? ' text-danger' : '') + (disabledReason ? ' disabled' : '');
    const attrs = disabledReason
        ? ` aria-disabled="true" title="${esc(disabledReason)}"`
        : ` data-action="${esc(action)}" data-id="${id}"`;
    return `<li><button type="button" class="${cls}"${attrs}>
        <i class="fas ${esc(icon)} fa-fw me-2" aria-hidden="true"></i>${esc(label)}</button></li>`;
}

function accessCell(u) {
    if (u.role === 'admin') {
        return '<span class="ae-badge is-admin"><i class="fas fa-infinity" aria-hidden="true"></i>All pipelines</span>';
    }
    const n = u.pipeline_access_count;
    if (n === null || n === undefined) return '<span class="text-muted small">—</span>';
    if (n === 0) return '<span class="text-muted small">No pipelines</span>';
    return `<span class="small">${n} pipeline${n === 1 ? '' : 's'}</span>`;
}

function rowHtml(u) {
    const isSelf = currentUserId === u.id;
    const lastAdmin = isLastActiveAdmin(u);

    const badges = [];
    if (isSelf) badges.push('<span class="ae-badge is-self ms-2"><i class="fas fa-circle-user" aria-hidden="true"></i>You</span>');
    if (u.must_change_password) badges.push(
        '<span class="ae-badge is-warn ms-2" title="Must set a new password at next login">' +
        '<i class="fas fa-key" aria-hidden="true"></i>Password change</span>');

    let menu = '<li><h6 class="ae-menu-label">Account</h6></li>';
    menu += menuItem({ action: 'edit', id: u.id, icon: 'fa-user-pen', label: 'Edit profile' });
    menu += menuItem({ action: 'access', id: u.id, icon: 'fa-diagram-project',
                       label: u.role === 'admin' ? 'Pipeline access (all)' : 'Pipeline access' });
    menu += '<li><hr class="dropdown-divider"></li><li><h6 class="ae-menu-label">Security</h6></li>';
    menu += menuItem({ action: 'reset', id: u.id, icon: 'fa-key', label: 'Reset password' });
    menu += menuItem({
        action: 'active', id: u.id, icon: u.is_active ? 'fa-user-slash' : 'fa-user-check',
        label: u.is_active ? 'Disable account' : 'Enable account',
        disabledReason: (u.is_active && lastAdmin) ? LAST_ADMIN_MSG.active : null });
    menu += '<li><hr class="dropdown-divider"></li><li><h6 class="ae-menu-label">Administration</h6></li>';
    menu += menuItem({
        action: 'role', id: u.id, icon: 'fa-user-shield',
        label: u.role === 'admin' ? 'Demote to user' : 'Promote to admin',
        disabledReason: (u.role === 'admin' && lastAdmin) ? LAST_ADMIN_MSG.role : null });
    menu += '<li><hr class="dropdown-divider"></li><li><h6 class="ae-menu-label">Danger</h6></li>';
    menu += menuItem({
        action: 'delete', id: u.id, icon: 'fa-trash', label: 'Delete user', danger: true,
        disabledReason: lastAdmin ? LAST_ADMIN_MSG.del : null });

    const roleBadge = u.role === 'admin'
        ? '<span class="ae-badge is-admin"><i class="fas fa-user-shield" aria-hidden="true"></i>Administrator</span>'
        : '<span class="ae-badge is-user"><i class="fas fa-user" aria-hidden="true"></i>Standard User</span>';
    const statusBadge = u.is_active
        ? '<span class="ae-badge is-active"><span class="ae-dot" aria-hidden="true">●</span>Active</span>'
        : '<span class="ae-badge is-disabled"><span class="ae-dot" aria-hidden="true">○</span>Disabled</span>';

    return `
<tr class="${u.is_active ? '' : 'ae-row-inactive'}">
  <td>
    <div class="ae-identity">
      <span class="ae-avatar ${u.role === 'admin' ? 'is-admin' : ''} ${u.is_active ? '' : 'is-inactive'}"
            aria-hidden="true">${esc(initials(u))}</span>
      <span class="ae-identity-lines">
        <span class="ae-primary">${esc(u.full_name || u.username)}</span>${badges.join('')}
        <span class="ae-handle d-block">@${esc(u.username)}</span>
        ${u.email ? `<span class="ae-mail d-block ae-truncate">${esc(u.email)}</span>` : ''}
      </span>
    </div>
  </td>
  <td class="ae-col-role">${roleBadge}</td>
  <td>${statusBadge}</td>
  <td class="ae-col-access">${accessCell(u)}</td>
  <td class="ae-col-lastlogin small text-muted" title="${esc(isoTitle(u.last_login))}">${esc(fmtUtc(u.last_login, { relative: true }))}</td>
  <td class="ae-col-created small text-muted" title="${esc(isoTitle(u.created_at))}">${esc(fmtUtc(u.created_at))}</td>
  <td class="text-end">
    <div class="dropdown">
      <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="dropdown"
              data-bs-boundary="viewport" aria-expanded="false"
              aria-label="Actions for ${esc(u.username)}">
        <i class="fas fa-ellipsis-vertical" aria-hidden="true"></i>
      </button>
      <ul class="dropdown-menu dropdown-menu-end">${menu}</ul>
    </div>
  </td>
</tr>`;
}

function renderStats() {
    const total = allUsers.length;
    const active = allUsers.filter(u => u.is_active).length;
    const admins = allUsers.filter(u => u.role === 'admin').length;
    dom['stat-total'].textContent = total;
    dom['stat-active'].textContent = active;
    dom['stat-admins'].textContent = admins;
    dom['stat-disabled'].textContent = total - active;
}

function renderUsers() {
    const rows = applyFilters();
    const countEl = dom['result-count'];
    const tbody = dom['users-tbody'];

    if (!allUsers.length) {
        lastRenderKey = 'empty';
        tbody.innerHTML = stateRow(COLS, {
            icon: 'fa-users', title: 'No user accounts yet',
            message: 'Create the first account to let someone sign in to this node.' });
        countEl.textContent = '';
        return;
    }
    if (!rows.length) {
        lastRenderKey = 'nomatch:' + JSON.stringify(filters);
        tbody.innerHTML = stateRow(COLS, {
            icon: 'fa-magnifying-glass', title: 'No users match these filters',
            message: 'Try a different search term or clear the filters.',
            actionLabel: 'Clear filters', actionId: 'btn-clear-filters' });
        const btn = document.getElementById('btn-clear-filters');
        if (btn) btn.addEventListener('click', clearFilters);
        countEl.textContent = `0 of ${allUsers.length}`;
        return;
    }

    // Diff-render: leave the DOM alone when nothing visible changed, so an open
    // dropdown is not destroyed by a no-op refresh.
    const key = JSON.stringify(rows) + '|' + activeAdminCount() + '|' + currentUserId;
    countEl.textContent = rows.length === allUsers.length
        ? `${rows.length} user${rows.length === 1 ? '' : 's'}`
        : `${rows.length} of ${allUsers.length}`;
    if (key === lastRenderKey) return;
    lastRenderKey = key;
    tbody.innerHTML = rows.map(rowHtml).join('');
}

function clearFilters() {
    filters = { search: '', role: 'all', status: 'all' };
    dom['user-search'].value = '';
    dom['filter-role'].value = 'all';
    dom['filter-status'].value = 'all';
    renderUsers();
}

/* ================================================================ loading */

async function loadUsers({ showSkeleton = true } = {}) {
    if (showSkeleton) {
        lastRenderKey = '';
        dom['users-tbody'].innerHTML = skeletonRows(5, COLS);
    }
    const res = await apiCall('/api/users');
    if (!res.ok) {
        lastRenderKey = '';
        allUsers = [];
        dom['users-tbody'].innerHTML = stateRow(COLS, {
            icon: 'fa-triangle-exclamation', variant: 'danger',
            title: 'Could not load users', message: res.error,
            actionLabel: 'Try again', actionId: 'btn-retry-users' });
        const btn = document.getElementById('btn-retry-users');
        if (btn) btn.addEventListener('click', () => loadUsers());
        dom['result-count'].textContent = '';
        return;
    }
    allUsers = (res.data && res.data.users) || [];
    lastRenderKey = '';
    renderStats();
    renderUsers();
}

/* ================================================================ actions */

function userById(id) { return allUsers.find(u => u.id === id); }

async function write(url, method, body, btn, busyLabel) {
    setButtonLoading(btn, true, busyLabel);
    const res = await apiCall(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    setButtonLoading(btn, false);
    if (!res.ok) showAlert('error', esc(res.error), 0);
    return res;
}

async function changeRole(u) {
    const toAdmin = u.role !== 'admin';
    const self = u.id === currentUserId;
    const ok = await confirmDialog({
        title: toAdmin ? 'Promote to administrator?' : 'Demote to standard user?',
        variant: toAdmin ? 'warning' : 'danger',
        body: toAdmin
            ? `<strong>${esc(u.username)}</strong> will gain full access, including user
               management, model uploads and engine creation.`
            : `<strong>${esc(u.username)}</strong> will lose access to user management,
               model uploads and engine creation.`,
        consequence: self && !toAdmin
            ? 'This is <strong>your own account</strong>. You will lose administrator access immediately and be signed out.'
            : 'This signs the user out of all active sessions.',
        confirmLabel: toAdmin ? 'Promote' : 'Demote',
    });
    if (!ok) return;
    const res = await write(`/api/users/${u.id}/role`, 'PUT', { role: toAdmin ? 'admin' : 'user' });
    if (res.ok) { showAlert('success', 'Role updated'); await loadUsers({ showSkeleton: false }); }
}

async function changeActive(u) {
    const enable = !u.is_active;
    const self = u.id === currentUserId;
    if (!enable) {
        const ok = await confirmDialog({
            title: 'Disable this account?',
            body: `<strong>${esc(u.username)}</strong> will no longer be able to sign in.
                   The account and its history are kept — you can re-enable it at any time.`,
            consequence: self
                ? 'This is <strong>your own account</strong>. You will be locked out immediately.'
                : 'This signs the user out of all active sessions.',
            confirmLabel: 'Disable account',
        });
        if (!ok) return;
    }
    // `active` is always sent explicitly: the backend reads bool(data.get("active")).
    const res = await write(`/api/users/${u.id}/active`, 'PUT', { active: enable });
    if (res.ok) {
        showAlert('success', enable ? 'Account enabled' : 'Account disabled');
        await loadUsers({ showSkeleton: false });
    }
}

async function deleteUser(u) {
    const self = u.id === currentUserId;
    const ok = await confirmDialog({
        title: 'Delete this user permanently?',
        body: `<strong>${esc(u.username)}</strong> will be removed from the directory.
               This cannot be undone. If you only want to block sign-in, disable the account instead.`,
        consequence: self
            ? 'This is <strong>your own account</strong>. You will be signed out and locked out immediately.'
            : null,
        requireText: u.username,
        confirmLabel: 'Delete user',
    });
    if (!ok) return;
    const res = await write(`/api/users/${u.id}`, 'DELETE', undefined);
    if (res.ok) { showAlert('success', 'User deleted'); await loadUsers({ showSkeleton: false }); }
}

/* ========================================================= role selector */

function renderRoleCards(container, selected) {
    container.innerHTML = ROLES.map(r => `
<button type="button" class="ae-role-card ${r.elevated ? 'is-elevated' : ''}"
        role="radio" aria-checked="${r.id === selected}" data-role="${esc(r.id)}"
        tabindex="${r.id === selected ? '0' : '-1'}">
  <span class="ae-role-title"><i class="fas ${esc(r.icon)}" aria-hidden="true"></i>${esc(r.title)}</span>
  <span class="ae-role-desc">${esc(r.desc)}</span>
</button>`).join('');

    const cards = Array.from(container.querySelectorAll('[data-role]'));
    const select = (card) => {
        cards.forEach(c => {
            const on = c === card;
            c.setAttribute('aria-checked', String(on));
            c.tabIndex = on ? 0 : -1;
        });
        container.dataset.value = card.dataset.role;
    };
    cards.forEach((card, i) => {
        card.addEventListener('click', () => select(card));
        card.addEventListener('keydown', ev => {
            if (ev.key === ' ' || ev.key === 'Enter') { ev.preventDefault(); select(card); return; }
            if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(ev.key)) {
                ev.preventDefault();
                const dir = (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') ? 1 : -1;
                const next = cards[(i + dir + cards.length) % cards.length];
                select(next); next.focus();
            }
        });
    });
    container.dataset.value = selected;
}

function selectedRole(container) { return container.dataset.value || 'user'; }

/* ============================================================ validation */

function setFieldError(input, errEl, message) {
    input.classList.toggle('is-invalid', !!message);
    if (errEl) errEl.textContent = message || '';
    return !message;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/* =========================================================== add user UX */

function resetCreateUserForm() {
    dom['add-user-form'].reset();
    ['au-username', 'au-password', 'au-confirm', 'au-email'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.remove('is-invalid');
    });
    ['au-username-err', 'au-password-err', 'au-confirm-err', 'au-email-err'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '';
    });
    // Restore password fields to masked, with the matching icon.
    ['au-password', 'au-confirm'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.type = 'password';
    });
    const toggle = document.getElementById('au-password-toggle');
    if (toggle) toggle.innerHTML = '<i class="fas fa-eye" aria-hidden="true"></i>';
    document.getElementById('au-mustchange').checked = true;
    renderRoleCards(document.getElementById('au-role-cards'), 'user');
    setButtonLoading(document.getElementById('au-submit'), false);
}

function openCreateUser() {
    resetCreateUserForm();
    modals.add.show();
}

async function createUser(ev) {
    ev.preventDefault();
    const username = document.getElementById('au-username');
    const password = document.getElementById('au-password');
    const confirm = document.getElementById('au-confirm');
    const email = document.getElementById('au-email');

    let ok = true;
    ok = setFieldError(username, document.getElementById('au-username-err'),
                       username.value.trim() ? '' : 'Username is required.') && ok;
    ok = setFieldError(password, document.getElementById('au-password-err'),
                       password.value.length >= 8 ? '' : 'Use at least 8 characters.') && ok;
    ok = setFieldError(confirm, document.getElementById('au-confirm-err'),
                       confirm.value === password.value ? '' : 'The two passwords do not match.') && ok;
    ok = setFieldError(email, document.getElementById('au-email-err'),
                       (!email.value.trim() || EMAIL_RE.test(email.value.trim()))
                           ? '' : 'Enter a valid email address.') && ok;
    if (!ok) return;

    const res = await write('/api/users', 'POST', {
        username: username.value.trim(),
        full_name: document.getElementById('au-fullname').value.trim(),
        email: email.value.trim(),
        password: password.value,
        role: selectedRole(document.getElementById('au-role-cards')),
        must_change_password: document.getElementById('au-mustchange').checked,
    }, document.getElementById('au-submit'), 'Creating…');
    if (!res.ok) return;

    modals.add.hide();
    showAlert('success', `User ${esc(username.value.trim())} created`);
    await loadUsers({ showSkeleton: false });
}

/* ============================================================== edit UX */

function openEditUser(u) {
    document.getElementById('eu-id').value = u.id;
    document.getElementById('eu-username').value = u.username;
    document.getElementById('eu-fullname').value = u.full_name || '';
    document.getElementById('eu-email').value = u.email || '';
    document.getElementById('eu-email').classList.remove('is-invalid');
    modals.edit.show();
}

async function updateUser(ev) {
    ev.preventDefault();
    const email = document.getElementById('eu-email');
    if (!setFieldError(email, document.getElementById('eu-email-err'),
                       (!email.value.trim() || EMAIL_RE.test(email.value.trim()))
                           ? '' : 'Enter a valid email address.')) return;

    const id = parseInt(document.getElementById('eu-id').value, 10);
    const res = await write(`/api/users/${id}`, 'PATCH', {
        full_name: document.getElementById('eu-fullname').value.trim(),
        email: email.value.trim(),
    }, document.getElementById('eu-submit'), 'Saving…');
    if (!res.ok) return;
    modals.edit.hide();
    showAlert('success', 'Profile updated');
    await loadUsers({ showSkeleton: false });
}

/* ==================================================== reset password UX */

function openPasswordReset(u) {
    document.getElementById('rp-id').value = u.id;
    document.getElementById('rp-username').textContent = u.username;
    ['rp-password', 'rp-confirm'].forEach(id => {
        const el = document.getElementById(id);
        el.value = '';
        el.type = 'password';
        el.classList.remove('is-invalid');
    });
    document.getElementById('rp-password-toggle').innerHTML = '<i class="fas fa-eye" aria-hidden="true"></i>';
    document.getElementById('rp-err').textContent = '';
    document.getElementById('rp-mustchange').checked = true;
    document.getElementById('rp-warning').innerHTML = u.id === currentUserId
        ? '<i class="fas fa-triangle-exclamation me-1" aria-hidden="true"></i>This is <strong>your own account</strong>. Resetting it signs you out immediately — you will need to sign in again with the new password.'
        : '<i class="fas fa-circle-info me-1" aria-hidden="true"></i>This ends all of that user\'s active sessions.';
    setButtonLoading(document.getElementById('rp-submit'), false);
    modals.reset.show();
}

async function resetPassword(ev) {
    ev.preventDefault();
    const pw = document.getElementById('rp-password');
    const confirm = document.getElementById('rp-confirm');
    const err = document.getElementById('rp-err');

    if (pw.value.length < 8) {
        confirm.classList.add('is-invalid');
        err.textContent = 'Use at least 8 characters.';
        return;
    }
    if (pw.value !== confirm.value) {
        confirm.classList.add('is-invalid');
        err.textContent = 'The two passwords do not match.';
        return;
    }
    confirm.classList.remove('is-invalid');

    const id = parseInt(document.getElementById('rp-id').value, 10);
    const res = await write(`/api/users/${id}/reset-password`, 'POST', {
        password: pw.value,
        must_change: document.getElementById('rp-mustchange').checked,
    }, document.getElementById('rp-submit'), 'Resetting…');
    if (!res.ok) return;
    modals.reset.hide();
    showAlert('success', 'Password reset. The user must sign in again.');
    await loadUsers({ showSkeleton: false });
}

/* ==================================================== pipeline access UX */

function accessRowHtml(p, grant) {
    const g = grant || {};
    const any = PERMS.some(perm => g[perm.key]);
    const implied = !!(g.can_start || g.can_stop || g.can_edit);
    return `
<div class="ae-access-row ${any ? 'is-granted' : ''}" data-pipeline="${esc(p.pipeline_id)}">
  <div class="ae-access-meta">
    <div class="ae-access-name">${esc(p.name || '(unnamed pipeline)')}</div>
    <div class="ae-access-sub">${esc(p.pipeline_id)}</div>
  </div>
  <div class="ae-perm-group" role="group" aria-label="Permissions for ${esc(p.name || p.pipeline_id)}">
    ${PERMS.map(perm => `
      <button type="button" class="ae-perm ${perm.key === 'can_view' && implied ? 'is-implied' : ''}"
              data-perm="${perm.key}" aria-pressed="${!!g[perm.key]}"
              ${perm.key === 'can_view' && implied ? 'disabled title="Implied by Start/Stop/Edit"' : ''}>
        ${esc(perm.label)}</button>`).join('')}
    <button type="button" class="btn btn-sm btn-outline-primary ms-1" data-save
            aria-label="Save access for ${esc(p.name || p.pipeline_id)}">
      <i class="fas fa-floppy-disk" aria-hidden="true"></i></button>
    <button type="button" class="btn btn-sm btn-outline-danger" data-revoke ${any ? '' : 'disabled'}
            aria-label="Revoke access for ${esc(p.name || p.pipeline_id)}">
      <i class="fas fa-xmark" aria-hidden="true"></i></button>
  </div>
</div>`;
}

async function openPipelineAccess(u) {
    document.getElementById('ac-username').textContent = u.username;
    document.getElementById('ac-admin-note').hidden = u.role !== 'admin';
    const body = document.getElementById('ac-body');
    body.innerHTML = '<span class="ae-skeleton" style="height:6rem"></span>';
    document.getElementById('ac-search').value = '';
    modals.access.show();

    const [pipesRes, accessRes] = await Promise.all([
        apiCall('/api/pipelines/assignable'),
        apiCall(`/api/users/${u.id}/pipeline-access`),
    ]);
    if (!pipesRes.ok || !accessRes.ok) {
        body.innerHTML = '';
        const div = document.createElement('div');
        div.className = 'alert alert-danger py-2 mb-0';
        div.textContent = (pipesRes.error || accessRes.error) || 'Could not load pipeline access.';
        body.appendChild(div);
        return;
    }

    const pipelines = (pipesRes.data && pipesRes.data.pipelines) || [];
    const granted = {};
    for (const a of (accessRes.data && accessRes.data.access) || []) granted[a.pipeline_id] = a;

    if (!pipelines.length) {
        body.innerHTML = '<div class="text-center text-muted py-4 small">No pipelines exist yet.</div>';
        return;
    }

    body.innerHTML = `<div class="ae-access-list">${
        pipelines.map(p => accessRowHtml(p, granted[p.pipeline_id])).join('')}</div>`;

    body.querySelectorAll('.ae-access-row').forEach(row => {
        const pid = row.dataset.pipeline;
        const perms = () => Array.from(row.querySelectorAll('[data-perm]'));
        const viewBtn = () => perms().find(b => b.dataset.perm === 'can_view');

        const syncImplied = () => {
            const implied = perms().some(b => b.dataset.perm !== 'can_view' &&
                                              b.getAttribute('aria-pressed') === 'true');
            const v = viewBtn();
            if (!v) return;
            v.classList.toggle('is-implied', implied);
            v.disabled = implied;
            if (implied) v.setAttribute('aria-pressed', 'true');
            v.title = implied ? 'Implied by Start/Stop/Edit' : '';
        };

        perms().forEach(btn => btn.addEventListener('click', () => {
            if (btn.disabled) return;
            btn.setAttribute('aria-pressed', String(btn.getAttribute('aria-pressed') !== 'true'));
            syncImplied();
        }));
        syncImplied();

        row.querySelector('[data-save]').addEventListener('click', async ev => {
            const payload = {};
            perms().forEach(b => { payload[b.dataset.perm] = b.getAttribute('aria-pressed') === 'true'; });
            const res = await write(`/api/pipelines/${encodeURIComponent(pid)}/access/${u.id}`,
                                    'PUT', payload, ev.currentTarget, '');
            if (res.ok) {
                showAlert('success', `Access updated for ${esc(u.username)}`);
                const any = PERMS.some(p => payload[p.key]);
                row.classList.toggle('is-granted', any);
                row.querySelector('[data-revoke]').disabled = !any;
                loadUsers({ showSkeleton: false });     // refresh the access count column
            }
        });

        row.querySelector('[data-revoke]').addEventListener('click', async ev => {
            const ok = await confirmDialog({
                title: 'Revoke access?',
                body: `<strong>${esc(u.username)}</strong> will lose all access to this pipeline.`,
                confirmLabel: 'Revoke', variant: 'danger',
            });
            if (!ok) return;
            const res = await write(`/api/pipelines/${encodeURIComponent(pid)}/access/${u.id}`,
                                    'DELETE', undefined, ev.currentTarget, '');
            if (res.ok) {
                perms().forEach(b => b.setAttribute('aria-pressed', 'false'));
                syncImplied();
                row.classList.remove('is-granted');
                row.querySelector('[data-revoke]').disabled = true;
                showAlert('success', 'Access revoked');
                loadUsers({ showSkeleton: false });
            }
        });
    });
}

function filterAccessRows(term) {
    const q = term.trim().toLowerCase();
    document.querySelectorAll('#ac-body .ae-access-row').forEach(row => {
        const text = row.querySelector('.ae-access-meta').textContent.toLowerCase();
        row.hidden = q ? !text.includes(q) : false;
    });
}

/* ============================================================== binding */

function bindEvents() {
    // Row action menus (delegated: usernames may contain quotes)
    dom['users-tbody'].addEventListener('click', ev => {
        const btn = ev.target.closest('[data-action]');
        if (!btn) return;
        const u = userById(parseInt(btn.dataset.id, 10));
        if (!u) return;
        switch (btn.dataset.action) {
            case 'edit':   openEditUser(u); break;
            case 'role':   changeRole(u); break;
            case 'active': changeActive(u); break;
            case 'access': openPipelineAccess(u); break;
            case 'reset':  openPasswordReset(u); break;
            case 'delete': deleteUser(u); break;
        }
    });

    dom['user-search'].addEventListener('input', debounce(ev => {
        filters.search = ev.target.value.trim();
        renderUsers();
    }, 200));
    dom['btn-clear-search'].addEventListener('click', () => {
        dom['user-search'].value = '';
        filters.search = '';
        renderUsers();
    });
    dom['filter-role'].addEventListener('change', ev => {
        filters.role = ev.target.value; renderUsers();
    });
    dom['filter-status'].addEventListener('change', ev => {
        filters.status = ev.target.value; renderUsers();
    });

    dom['btn-refresh'].addEventListener('click', () => loadUsers());
    dom['btn-add-user'].addEventListener('click', openCreateUser);

    attachPasswordToggle('au-password', 'au-password-toggle');
    attachPasswordToggle('rp-password', 'rp-password-toggle');

    document.getElementById('au-password-gen').addEventListener('click', () => {
        const pw = suggestPassword();
        const p = document.getElementById('au-password');
        const c = document.getElementById('au-confirm');
        p.value = c.value = pw;
        p.type = c.type = 'text';
        document.getElementById('au-password-toggle').innerHTML = '<i class="fas fa-eye-slash" aria-hidden="true"></i>';
        p.classList.remove('is-invalid'); c.classList.remove('is-invalid');
        document.getElementById('au-password-err').textContent = '';
        document.getElementById('au-confirm-err').textContent = '';
    });
    document.getElementById('rp-password-gen').addEventListener('click', () => {
        const pw = suggestPassword();
        const p = document.getElementById('rp-password');
        const c = document.getElementById('rp-confirm');
        p.value = c.value = pw;
        p.type = 'text';
        document.getElementById('rp-password-toggle').innerHTML = '<i class="fas fa-eye-slash" aria-hidden="true"></i>';
        c.classList.remove('is-invalid');
        document.getElementById('rp-err').textContent = '';
    });

    document.getElementById('ac-search').addEventListener('input',
        debounce(ev => filterAccessRows(ev.target.value), 150));

    dom['add-user-form'].addEventListener('submit', createUser);
    dom['edit-user-form'].addEventListener('submit', updateUser);
    dom['reset-pw-form'].addEventListener('submit', resetPassword);

    // Modal lifecycle: focus the first meaningful field, and never leave a password
    // sitting in the DOM after the dialog closes.
    dom['addUserModal'].addEventListener('shown.bs.modal',
        () => document.getElementById('au-username').focus());
    dom['editUserModal'].addEventListener('shown.bs.modal',
        () => document.getElementById('eu-fullname').focus());
    dom['resetPwModal'].addEventListener('shown.bs.modal',
        () => document.getElementById('rp-password').focus());

    dom['addUserModal'].addEventListener('hidden.bs.modal', () => {
        ['au-password', 'au-confirm'].forEach(id => {
            const el = document.getElementById(id);
            el.value = ''; el.type = 'password';
        });
    });
    dom['resetPwModal'].addEventListener('hidden.bs.modal', () => {
        ['rp-password', 'rp-confirm'].forEach(id => {
            const el = document.getElementById(id);
            el.value = ''; el.type = 'password';
        });
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

/* Narrow surface for the escaping / filtering regression tests
   (tests/admin_users_render.test.mjs). The page itself never reads this; it exists so
   the XSS guard can exercise the REAL row renderer rather than a copy of it. */
window.__adminUsersTestApi = {
    rowHtml,
    applyFilters,
    setUsers(users) { allUsers = users; },
    setFilters(f) { filters = f; },
    setCurrentUserId(id) { currentUserId = id; },
};

})();
