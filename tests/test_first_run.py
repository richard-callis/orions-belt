"""
Regression test for the first-run model-download page's Skip button.

The Skip button uses an inline onclick="skipSetup()" attribute, which
resolves identifiers in the GLOBAL scope — but the page's whole script is
wrapped in an IIFE `(function () { ... })();`, so a plain
`async function skipSetup() {...}` declared inside it is invisible outside
that closure. Clicking Skip threw a silent ReferenceError (visible only in
the browser console) and never called /api/first-run/skip or navigated
away, leaving the user stuck on the download screen with no feedback at
all. skipSetup must be exposed on `window` for the inline handler to reach it.
"""
import re

from app.routes import first_run as first_run_mod


class TestFirstRunSkipButtonWiring:
    def test_skip_button_calls_a_globally_exposed_function(self, app, client):
        resp = client.get("/first-run")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)

        assert 'onclick="skipSetup()"' in html, "expected the Skip button to call skipSetup()"
        assert re.search(r"window\.skipSetup\s*=\s*skipSetup", html), (
            "skipSetup() is declared inside the page's IIFE and is not exposed on `window` — "
            "the inline onclick handler resolves in global scope, so it can't reach a "
            "closure-local function. Clicking Skip would throw a silent ReferenceError."
        )

    def test_skip_route_marks_done_and_writes_sentinel(self, app, client, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "BASE_DIR", tmp_path)
        with first_run_mod._lock:
            first_run_mod._state["skipped"] = False
            first_run_mod._state["done"] = False

        resp = client.post("/api/first-run/skip")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["skipped"] is True
        with first_run_mod._lock:
            assert first_run_mod._state["done"] is True
        assert (tmp_path / first_run_mod._SKIP_SENTINEL).exists()
