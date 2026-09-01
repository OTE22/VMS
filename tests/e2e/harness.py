"""Shared E2E helpers (imported by conftest and tests; not a conftest itself)."""
import json

E2E_ADMIN = "e2e_admin"
E2E_ADMIN_PASSWORD = "E2e-Initial-Pass-2026!"
E2E_ADMIN_NEW_PASSWORD = "E2e-Rotated-Pass-2026!"


class ConsoleLog:
    def __init__(self):
        self.errors = []
        self.page_errors = []
        self.failed_requests = []

    def attach(self, page):
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        page.on("requestfailed", lambda r: self.failed_requests.append(f"{r.method} {r.url} {r.failure}"))
        return self

    def app_errors(self):
        # favicon 404 noise is not an application error
        return [e for e in self.errors + self.page_errors if "favicon" not in e.lower()]


def csrf_token(page) -> str:
    """CSRF token from the current page's <meta>; navigates to the dashboard first when
    the page has not loaded an application page yet (about:blank)."""
    tok = page.evaluate("() => (document.querySelector('meta[name=csrf-token]') || {}).content || ''")
    if not tok:
        page.goto("/"); page.wait_for_load_state("domcontentloaded")
        tok = page.evaluate("() => (document.querySelector('meta[name=csrf-token]') || {}).content || ''")
    return tok


def api(page, method: str, path: str, data=None, files=None):
    """Same-origin API call from the logged-in browser context (CSRF header included)."""
    headers = {"X-CSRFToken": csrf_token(page)}
    kw = {"headers": headers}
    if files is not None:
        kw["multipart"] = files
    elif data is not None:
        kw["data"] = json.dumps(data); headers["Content-Type"] = "application/json"
    return getattr(page.request, method.lower())(path, **kw)
