"""Suite-wide test environment isolation."""

import pytest

from archaeon.cost import ROUTE_VARS


@pytest.fixture(autouse=True)
def _neutral_billing_route_env(monkeypatch):
    """Hide the developer's own Claude routing env from every test.

    ``archaeon.cost`` reports ``billed: null`` when any of
    CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_VERTEX / ANTHROPIC_BASE_URL is
    set, because the billing route is then unknowable from the Python side.
    Those vars are routinely set in a real shell, which would otherwise flip
    the subscription-branch assertions in tests/test_cost.py and
    tests/test_cli.py depending on who runs the suite. Tests that want the
    override branch set the var themselves after this fixture runs.
    """
    for var in ROUTE_VARS:
        monkeypatch.delenv(var, raising=False)
