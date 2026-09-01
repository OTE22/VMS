/*
 * Shared behaviour for the ArmyEye authentication screens (login, change password).
 *
 * ONE module for both pages - it initialises only the controls that exist on the
 * current page, so login (username + password) and change-password (current + new +
 * confirm) share a single implementation instead of duplicating it per template.
 *
 * The forms stay ordinary server-driven <form method="POST"> submissions. This file
 * only improves UX; the server remains solely responsible for authentication, rate
 * limiting, safe redirects, session creation and must-change-password decisions.
 * No credential is ever logged, stored, or sent anywhere by this code.
 *
 * Deliberately depends on nothing else - no app.js, no armyeye-ui.js, no Bootstrap JS.
 */
'use strict';

(function () {

/* ------------------------------------------------------- password visibility */

function wirePasswordToggle(toggle) {
    const input = document.getElementById(toggle.dataset.toggleFor || '');
    if (!input) return;

    const icon = toggle.querySelector('i');
    const apply = (visible) => {
        input.type = visible ? 'text' : 'password';
        toggle.setAttribute('aria-pressed', String(visible));
        // The accessible name must describe the ACTION, and flip with the state.
        toggle.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
        toggle.title = visible ? 'Hide password' : 'Show password';
        if (icon) icon.className = visible ? 'fas fa-eye-slash' : 'fas fa-eye';
    };

    apply(false);
    toggle.addEventListener('click', () => {
        const nowVisible = input.type === 'password';
        apply(nowVisible);
        input.focus();
    });
}

/* ------------------------------------------------------------ caps lock hint */

function wireCapsLockHint(input) {
    const hint = document.getElementById(input.dataset.capsHint || '');
    if (!hint) return;

    const update = (ev) => {
        // getModifierState is unavailable on some synthetic events; stay silent then.
        let on = false;
        try { on = ev.getModifierState && ev.getModifierState('CapsLock'); } catch (_) { on = false; }
        hint.hidden = !on;
    };
    input.addEventListener('keyup', update);
    input.addEventListener('keydown', update);
    input.addEventListener('blur', () => { hint.hidden = true; });
}

/* --------------------------------------------------------------- submit UX */

function wireForm(form) {
    const submit = form.querySelector('[type="submit"]');
    let submitting = false;

    form.addEventListener('submit', (ev) => {
        // Double-submit prevention: a second Enter/click must not fire another request.
        if (submitting) {
            ev.preventDefault();
            return;
        }

        // Required-field UX only. The server validates for real.
        const missing = Array.from(form.querySelectorAll('[required]'))
            .filter(el => !el.value.trim());
        if (missing.length) {
            ev.preventDefault();
            missing.forEach(el => el.classList.add('is-invalid'));
            missing[0].focus();
            return;
        }

        submitting = true;
        if (submit) {
            submit.disabled = true;
            submit.dataset.originalHtml = submit.innerHTML;
            submit.innerHTML =
                '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>' +
                (submit.dataset.busyLabel || 'Signing in…');
        }
        // A normal form POST follows. If the browser restores this page from bfcache
        // (back button), pageshow below puts the button back.
    });

    // Clear the invalid state as soon as the user starts fixing it.
    form.querySelectorAll('[required]').forEach(el => {
        el.addEventListener('input', () => el.classList.remove('is-invalid'));
    });

    // Restore the button if the page is shown again from the back/forward cache -
    // otherwise the user returns to a permanently disabled, spinning button.
    window.addEventListener('pageshow', (ev) => {
        if (!ev.persisted && !submitting) return;
        submitting = false;
        if (submit && submit.dataset.originalHtml) {
            submit.disabled = false;
            submit.innerHTML = submit.dataset.originalHtml;
            delete submit.dataset.originalHtml;
        }
    });
}

/* ------------------------------------------------------------------- init */

function init() {
    document.querySelectorAll('[data-toggle-for]').forEach(wirePasswordToggle);
    document.querySelectorAll('[data-caps-hint]').forEach(wireCapsLockHint);
    document.querySelectorAll('form[data-auth-form]').forEach(wireForm);

    // Focus the first empty field so a failed attempt lands the cursor usefully
    // (username if it was cleared, otherwise the password the user must retype).
    const focusTarget = document.querySelector('[data-autofocus]');
    if (focusTarget && !focusTarget.value) {
        focusTarget.focus();
    } else {
        const firstEmpty = Array.from(document.querySelectorAll('form[data-auth-form] [required]'))
            .find(el => !el.value);
        if (firstEmpty) firstEmpty.focus();
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

})();
