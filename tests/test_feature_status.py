"""smriti_update_feature_status says "Moved" exactly when the spec changes directory (H11.5)."""

from __future__ import annotations

import pytest

from smriti.core.tree import tree_root
from smriti.mcp.tools.projects import handle_record_feature, handle_update_feature_status


@pytest.fixture(autouse=True)
def feature() -> None:
    (tree_root() / "mirrors" / "demo" / "ai").mkdir(parents=True)
    handle_record_feature({"project": "demo", "slug": "lamps", "title": "Lamps",
                           "goal": "Keep the lamps lit.", "plan": "Oil, wick, flame."})


def _update(status: str) -> str:
    return handle_update_feature_status({"project": "demo", "slug": "lamps",
                                         "new_status": status})


def _where() -> list[str]:
    base = tree_root() / "mirrors" / "demo" / "ai" / "features"
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*.md"))


@pytest.mark.parametrize("status", ["shipped", "abandoned"])
def test_a_move_out_of_active_says_moved(status: str) -> None:
    """It said "Updated" here: the status and the directory share a name."""
    assert _update(status) == f"Moved feature lamps: active/ -> {status}/ (status={status})"
    assert _where() == [f"{status}/lamps.md"]


def test_a_status_change_inside_active_says_updated() -> None:
    assert _update("building") == "Updated feature lamps -> status=building (in active/)"
    assert _where() == ["active/lamps.md"]


def test_a_move_back_and_a_repeat() -> None:
    _update("shipped")
    assert _update("shipped") == "Updated feature lamps -> status=shipped (in shipped/)"
    assert _update("reviewing") == (
        "Moved feature lamps: shipped/ -> active/ (status=reviewing)")
    assert _where() == ["active/lamps.md"]
