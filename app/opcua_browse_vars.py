from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Optional

BrowseFn = Callable[[Optional[str]], Awaitable[list[Any]]]

_SKIP_CLASSES = {"Method", "View"}
_CONTAINER_CLASSES = {"Object", "ObjectType", "Folder"}


async def walk_variables(
    browse: BrowseFn,
    start_id: Optional[str],
    *,
    start_item: Any = None,
    max_nodes: int = 200,
    max_depth: int = 10,
) -> tuple[list[Any], bool]:
    """선택한 노드 아래 Variable 을 재귀로 모은다. Variable 자체면 그 노드만 반환한다."""
    if start_item is not None and start_item.node_class == "Variable":
        return [start_item], False

    found: list[Any] = []
    seen: set[str] = set()
    queue: list[tuple[Optional[str], int]] = [(start_id, 0)]
    truncated = False

    while queue:
        node_id, depth = queue.pop(0)
        if node_id is not None:
            if node_id in seen:
                continue
            seen.add(node_id)
        children = await browse(node_id)
        for child in children:
            if child.node_id in seen:
                continue
            if child.node_class == "Variable":
                seen.add(child.node_id)
                found.append(child)
                if len(found) >= max_nodes:
                    return found, True
                continue
            if child.node_class in _SKIP_CLASSES:
                seen.add(child.node_id)
                continue
            if depth + 1 >= max_depth:
                truncated = True
                continue
            if child.node_class in _CONTAINER_CLASSES or not child.node_class:
                queue.append((child.node_id, depth + 1))
    return found, truncated
