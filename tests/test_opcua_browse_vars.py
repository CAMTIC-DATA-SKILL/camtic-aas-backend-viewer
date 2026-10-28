from types import SimpleNamespace

from app.opcua_browse_vars import walk_variables


def _item(node_id: str, node_class: str, name: str | None = None) -> SimpleNamespace:
    label = name or node_id
    return SimpleNamespace(node_id=node_id, browse_name=label, display_name=label, node_class=node_class)


def test_walk_variables_returns_variable_itself():
    start = _item("ns=2;s=rpm", "Variable", "rpm")

    async def browse(_node_id):
        raise AssertionError("Variable 은 하위를 조회하지 않아야 합니다.")

    items, truncated = _run(walk_variables(browse, start.node_id, start_item=start))
    assert [item.node_id for item in items] == ["ns=2;s=rpm"]
    assert truncated is False


def test_walk_variables_collects_descendants_and_skips_methods():
    tree = {
        "Objects": [_item("robot", "Object")],
        "robot": [
            _item("robot-1", "Object"),
            _item("reset", "Method"),
        ],
        "robot-1": [
            _item("rpm", "Variable"),
            _item("temp", "Variable"),
            _item("ident", "Object"),
        ],
        "ident": [_item("vendor", "Variable")],
    }

    items, truncated = _run(walk_variables(_sync_browse(tree), "robot"))
    assert [item.node_id for item in items] == ["rpm", "temp", "vendor"]
    assert truncated is False


def test_walk_variables_respects_max_nodes():
    tree = {
        "folder": [_item(f"v{i}", "Variable") for i in range(5)],
    }
    items, truncated = _run(walk_variables(_sync_browse(tree), "folder", max_nodes=2))
    assert len(items) == 2
    assert truncated is True


def _sync_browse(tree):
    async def browse(node_id):
        return list(tree.get(node_id or "Objects", []))

    return browse


def _run(coro):
    import asyncio

    return asyncio.run(coro)
