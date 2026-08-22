import importlib.util

from tests.conftest import ROOT


def test_default_watchlist_js_is_current():
    spec = importlib.util.spec_from_file_location(
        "export_watchlist", ROOT / "scripts/export_watchlist.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (ROOT / "extension/shared/watchlist.default.js").read_text() == mod.render(), (
        "run: uv run python scripts/export_watchlist.py"
    )
