/*
 * ArmyEye shared admin UI helpers.
 *
 * Small, dependency-free additions on top of app.js (fetchJSON / showAlert) and
 * Bootstrap 5.3. Everything here is used by BOTH admin pages - do not duplicate
 * these per page.
 */

/* ---------------------------------------------------------------- escaping */

/** Escape a value for safe interpolation into innerHTML / attributes.
 *  app.js has no escaper and several pages interpolate server data straight into
 *  innerHTML - every dynamic string in the admin pages must go through this. */
function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/* -------------------------------------------------------------- timestamps */

/** Parse a backend timestamp. The auth tables store NAIVE UTC and isoformat()
 *  emits no 'Z', so a bare `new Date(s)` is parsed as local time and renders
 *  hours off. Append the Z when there is no explicit zone. */
function parseUtc(value) {
    if (!value) return null;
    const s = String(value);
    const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
    const d = new Date(hasZone ? s : s + 'Z');
    return isNaN(d.getTime()) ? null : d;
}

/** Human-readable local time for a backend UTC timestamp ('—' when absent). */
function fmtUtc(value, { relative = false } = {}) {
    const d = parseUtc(value);
    if (!d) return '—';
    if (!relative) return d.toLocaleString();
    const diff = Date.now() - d.getTime();
    const mins = Math.round(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.round(hrs / 24);
    if (days < 30) return `${days}d ago`;
    return d.toLocaleDateString();
}

/** ISO string for a `title=` tooltip. */
function isoTitle(value) {
    const d = parseUtc(value);
    return d ? d.toISOString() : '';
}

/* ------------------------------------------------------------- API calling */

/** Wrapper over fetchJSON that returns {ok, status, data, error}.
 *
 *  Handles the real backend behaviours:
 *   - CSRF failures return an HTML 400 body (abort(400)), so r.json() throws
 *   - /validate returns HTTP 200 with {valid:false} - callers branch on data
 *   - 401 is already redirected to /login by fetchJSON
 *  `error` is always a human-readable sentence, never a bare status code. */
async function apiCall(url, options = {}) {
    let response;
    try {
        response = await fetchJSON(url, options);
    } catch (e) {
        if (e && e.message === 'Not authenticated') throw e;   // fetchJSON is redirecting
        return { ok: false, status: 0, data: null,
                 error: 'Could not reach the server. Check your connection and try again.',
                 detail: e ? e.message : '' };
    }

    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }

    if (response.ok) return { ok: true, status: response.status, data, error: null };

    // Non-JSON body (e.g. Flask's HTML 400 page from a CSRF abort)
    const serverMsg = data && data.error ? data.error : null;
    let error = serverMsg;
    if (!error) {
        switch (response.status) {
            case 400: error = 'The request was rejected. Your session token may have expired — reload the page and try again.'; break;
            case 403: error = 'You do not have permission to perform this action.'; break;
            case 404: error = 'Not found. This feature may be disabled on this node.'; break;
            case 409: error = 'That item already exists.'; break;
            case 429: error = 'Too many attempts. Wait a moment and try again.'; break;
            default:  error = response.status >= 500
                ? 'The server could not complete the request. Check the node logs.'
                : `Request failed (HTTP ${response.status}).`;
        }
    }
    return { ok: false, status: response.status, data, error,
             detail: serverMsg ? '' : (text || '').slice(0, 400) };
}

/* ------------------------------------------------------------- button state */

/** Toggle a button into/out of a loading state, preserving its markup. */
function setButtonLoading(btn, loading, loadingLabel = 'Working…') {
    if (!btn) return;
    if (loading) {
        if (!btn.dataset.originalHtml) btn.dataset.originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        btn.innerHTML = `<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>${esc(loadingLabel)}`;
    } else {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        if (btn.dataset.originalHtml) {
            btn.innerHTML = btn.dataset.originalHtml;
            delete btn.dataset.originalHtml;
        }
    }
}

/* --------------------------------------------------------- confirm dialog */

/** Promise-based confirmation modal replacing native confirm()/prompt().
 *  Focus-trapped by Bootstrap, Esc closes, focus returns to the trigger.
 *  Resolves true (confirmed) / false (dismissed). */
function confirmDialog({ title, body, confirmLabel = 'Confirm', cancelLabel = 'Cancel',
                         variant = 'danger', requireText = null, consequence = null } = {}) {
    return new Promise(resolve => {
        const trigger = document.activeElement;
        const id = 'confirmDlg_' + Math.random().toString(36).slice(2, 9);
        const wrap = document.createElement('div');
        wrap.innerHTML = `
<div class="modal fade" id="${id}" tabindex="-1" role="dialog"
     aria-labelledby="${id}_title" aria-hidden="true">
  <div class="modal-dialog modal-dialog-centered" role="document">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="${id}_title">
          <i class="fas fa-triangle-exclamation me-2 text-${variant === 'danger' ? 'danger' : 'warning'}"></i>${esc(title || 'Are you sure?')}
        </h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <p class="mb-2">${body || ''}</p>
        ${consequence ? `<div class="alert alert-warning py-2 small mb-0"><i class="fas fa-circle-info me-1"></i>${consequence}</div>` : ''}
        ${requireText ? `
        <label class="form-label mt-3" for="${id}_confirmInput">
          Type <code>${esc(requireText)}</code> to confirm
        </label>
        <input class="form-control" id="${id}_confirmInput" autocomplete="off" spellcheck="false">` : ''}
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">${esc(cancelLabel)}</button>
        <button type="button" class="btn btn-${variant}" id="${id}_ok" ${requireText ? 'disabled' : ''}>${esc(confirmLabel)}</button>
      </div>
    </div>
  </div>
</div>`;
        document.body.appendChild(wrap);
        const el = wrap.querySelector('.modal');
        const okBtn = wrap.querySelector('#' + id + '_ok');
        const input = requireText ? wrap.querySelector('#' + id + '_confirmInput') : null;
        let confirmed = false;

        if (input) {
            input.addEventListener('input', () => { okBtn.disabled = input.value.trim() !== requireText; });
            input.addEventListener('keydown', e => {
                if (e.key === 'Enter' && !okBtn.disabled) okBtn.click();
            });
        }
        okBtn.addEventListener('click', () => {
            confirmed = true;
            bootstrap.Modal.getInstance(el).hide();
        });
        el.addEventListener('shown.bs.modal', () => (input || okBtn).focus());
        el.addEventListener('hidden.bs.modal', () => {
            wrap.remove();
            if (trigger && trigger.focus) trigger.focus();
            resolve(confirmed);
        });
        new bootstrap.Modal(el).show();
    });
}

/* --------------------------------------------------------------- states */

/** Skeleton rows for a loading table. */
function skeletonRows(rows, cols) {
    let html = '';
    for (let r = 0; r < rows; r++) {
        html += '<tr aria-hidden="true">';
        for (let c = 0; c < cols; c++) html += '<td><span class="ae-skeleton"></span></td>';
        html += '</tr>';
    }
    return html;
}

/** Full-width message row for a table body. */
function stateRow(cols, { icon, title, message, actionLabel, actionId, variant = 'muted' }) {
    return `
<tr><td colspan="${cols}">
  <div class="text-center py-5">
    <i class="fas ${esc(icon)} fa-2x mb-3 text-${variant === 'danger' ? 'danger' : 'secondary'}" aria-hidden="true"></i>
    <h6 class="mb-1">${esc(title)}</h6>
    ${message ? `<p class="text-muted small mb-3">${esc(message)}</p>` : ''}
    ${actionLabel ? `<button class="btn btn-sm btn-outline-primary" id="${esc(actionId)}">
        <i class="fas fa-rotate me-1"></i>${esc(actionLabel)}</button>` : ''}
  </div>
</td></tr>`;
}

/** Random password suggestion (client-side only; server policy is length >= 8). */
function suggestPassword(length = 16) {
    const chars = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*';
    const out = new Uint32Array(length);
    crypto.getRandomValues(out);
    return Array.from(out, n => chars[n % chars.length]).join('');
}

/** Wire a show/hide toggle onto a password input. */
function attachPasswordToggle(inputId, buttonId) {
    const input = document.getElementById(inputId);
    const btn = document.getElementById(buttonId);
    if (!input || !btn) return;
    btn.addEventListener('click', () => {
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        btn.innerHTML = `<i class="fas fa-eye${show ? '-slash' : ''}"></i>`;
        btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
        input.focus();
    });
}
