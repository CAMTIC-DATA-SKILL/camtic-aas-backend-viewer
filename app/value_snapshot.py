"""다운로드 시점에만 쓰는 AAS 사본. 서버에 올라간 패키지의 Property 는 바꾸지 않는다."""

from __future__ import annotations

import io
import re
from typing import Any, Optional

from basyx.aas import model
from basyx.aas.adapter.json import object_store_to_json, read_aas_json_file
from basyx.aas.model import datatypes

from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)

_SEGMENT = re.compile(r"^([^[\]]+)?(?:\[(\d+)\])?$")


def clone_object_store(objects: model.DictIdentifiableStore) -> model.DictIdentifiableStore:
    payload = object_store_to_json(objects)
    return read_aas_json_file(io.StringIO(payload), failsafe=False)


def capture_object_store(
    objects: model.DictIdentifiableStore, values: list[dict[str, Any]]
) -> tuple[Optional[model.DictIdentifiableStore], int]:
    """usable 한 현장 값을 Property 에 넣은 사본과 반영 개수. 반영할 값이 없으면 (None, 0)."""
    if not values:
        return None, 0
    store = clone_object_store(objects)
    applied = apply_captured_values(store, values)
    if not applied:
        return None, 0
    logger.info("Captured %d property values for download", applied)
    return store, applied


def apply_captured_values(store: model.DictIdentifiableStore, values: list[dict[str, Any]]) -> int:
    submodels: dict[str, list[model.Submodel]] = {}
    for obj in store:
        if isinstance(obj, model.Submodel) and obj.id_short:
            submodels.setdefault(obj.id_short, []).append(obj)

    applied = 0
    for item in values:
        submodel_name = str(item.get("submodelIdShort") or "")
        path = str(item.get("path") or "")
        raw = item.get("value")
        if not submodel_name or not path or raw is None:
            continue
        for submodel in submodels.get(submodel_name, []):
            element = _element_at(submodel, path)
            if not isinstance(element, model.Property):
                logger.warning("Snapshot skip %s/%s: Property 가 아닙니다.", submodel_name, path)
                continue
            try:
                element.value = _coerce_property_value(raw, element.value_type)
            except (TypeError, ValueError) as exc:
                logger.warning("Snapshot skip %s/%s: %s", submodel_name, path, exc)
                continue
            applied += 1
    return applied


def _coerce_property_value(value: Any, value_type: type) -> Any:
    if isinstance(value, bool) and value_type is bool:
        return value
    try:
        return datatypes.trivial_cast(value, value_type)
    except TypeError:
        pass
    if isinstance(value, bool):
        if issubclass(value_type, str):
            return datatypes.trivial_cast("true" if value else "false", value_type)
        raise TypeError(f"{value!r} cannot be stored as {value_type.__name__}")
    if isinstance(value, int) and issubclass(value_type, float):
        return datatypes.trivial_cast(float(value), value_type)
    if (
        isinstance(value, float)
        and issubclass(value_type, int)
        and not issubclass(value_type, bool)
        and value.is_integer()
    ):
        return datatypes.trivial_cast(int(value), value_type)
    if isinstance(value, str):
        return datatypes.from_xsd(value, value_type)
    if issubclass(value_type, str):
        return datatypes.trivial_cast(str(value), value_type)
    raise TypeError(f"{value!r} cannot be stored as {value_type.__name__}")


def _element_at(submodel: model.Submodel, path: str) -> Optional[model.SubmodelElement]:
    node: Any = submodel
    for raw in path.split("."):
        if not raw:
            return None
        match = _SEGMENT.fullmatch(raw)
        if match is None:
            return None
        name, index_text = match.group(1), match.group(2)
        if name:
            node = _child_named(_children(node), name)
            if node is None:
                return None
        if index_text is not None:
            children = _children(node)
            index = int(index_text)
            if index >= len(children):
                return None
            node = children[index]
        elif not name:
            return None
    return node if isinstance(node, model.SubmodelElement) else None


def _children(node: Any) -> list[model.SubmodelElement]:
    if isinstance(node, model.Submodel):
        return list(node.submodel_element)
    if isinstance(node, (model.SubmodelElementCollection, model.SubmodelElementList)):
        return list(node.value)
    if isinstance(node, model.Entity):
        return list(node.statement)
    if isinstance(node, model.AnnotatedRelationshipElement):
        return list(node.annotation)
    return []


def _child_named(
    children: list[model.SubmodelElement], name: str
) -> Optional[model.SubmodelElement]:
    for child in children:
        if child.id_short == name:
            return child
    return None
