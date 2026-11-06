"""AASX Documentation Submodel 조회·DT 모델링 파일 첨부."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional
from urllib.parse import quote

from basyx.aas import model

from .repository import AasxPackage
from .serializers import encode_id

FileKind = Literal["step", "usd", "file"]
FileUrlResolver = Callable[[str], str]
TwinFileStore = Callable[[str, str, bytes], Optional[Path]]

_DOC_ID_SHORTS = {"documentation", "handoverdocumentation"}
_DOC_SEMANTIC_HINTS = (
    "0173-1#01-ahf578",
    "handoverdocumentation",
    "handover-documentation",
    "/documentation",
    ":documentation",
)
_STEP_EXTS = {".stp", ".step"}
_USD_EXTS = {".usd", ".usda", ".usdc", ".usdz"}


@dataclass(frozen=True)
class MainFileSlot:
    kind: FileKind
    id_short: str
    semantic_id: str
    aliases: frozenset[str]
    content_type: str
    display_ko: str
    display_en: str

    @property
    def names(self) -> frozenset[str]:
        return self.aliases | {self.id_short.lower()}


MAIN_CAD_SLOT = MainFileSlot(
    kind="step",
    id_short="DigitalTwinCadModel",
    semantic_id="urn:physical-ai:ids/cd/DigitalTwinCadModel/1/0",
    aliases=frozenset({"step", "stepmodel", "stepfile", "digitaltwincadmodel"}),
    content_type="application/step",
    display_ko="디지털 트윈 CAD 모델",
    display_en="Digital Twin CAD Model",
)
MAIN_USD_SLOT = MainFileSlot(
    kind="usd",
    id_short="DigitalTwinUsdModel",
    semantic_id="urn:physical-ai:ids/cd/DigitalTwinUsdModel/1/0",
    aliases=frozenset({"usd", "usdmodel", "usdfile", "digitaltwinusdmodel"}),
    content_type="model/vnd.usda+ascii",
    display_ko="디지털 트윈 USD 모델",
    display_en="Digital Twin USD Model",
)
MAIN_FILE_SLOTS = (MAIN_CAD_SLOT, MAIN_USD_SLOT)
MAIN_FILE_ID_SHORTS = {slot.id_short for slot in MAIN_FILE_SLOTS}
MAIN_FILE_SEMANTIC_IDS = {slot.semantic_id: slot for slot in MAIN_FILE_SLOTS}
_CONTENT_TYPES = {
    ".stp": "application/step",
    ".step": "application/step",
    ".usd": "model/vnd.usda+ascii",
    ".usda": "model/vnd.usda+ascii",
    ".usdc": "model/vnd.usdc",
    ".usdz": "model/vnd.usdz+zip",
}
_SAFE_FILENAME = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)
_SAFE_ID_SHORT = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class Attachment:
    kind: FileKind
    filename: str
    content: bytes
    content_type: str = ""
    id_short: str = ""


def kind_from_filename(filename: str) -> FileKind:
    ext = Path(filename).suffix.lower()
    if ext in _STEP_EXTS:
        return "step"
    if ext in _USD_EXTS:
        return "usd"
    return "file"


def content_type_for(filename: str, uploaded_type: str = "") -> str:
    ext = Path(filename).suffix.lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    if uploaded_type and uploaded_type != "application/octet-stream":
        return uploaded_type
    return "application/octet-stream"


def find_documentation_submodels(package: AasxPackage) -> list[model.Submodel]:
    found: list[model.Submodel] = []
    for submodel in package.submodels:
        id_short = (submodel.id_short or "").lower()
        semantic = _semantic_text(submodel).lower()
        identity = submodel.id.lower()
        if (
            id_short in _DOC_ID_SHORTS
            or any(hint in semantic for hint in _DOC_SEMANTIC_HINTS)
            or any(hint in identity for hint in _DOC_SEMANTIC_HINTS)
        ):
            found.append(submodel)
    return found


def serialize_documentation(package: AasxPackage, file_url: FileUrlResolver) -> dict[str, Any]:
    submodels = find_documentation_submodels(package)
    return {
        "packageId": package.id,
        "fileName": package.path.name,
        "created": False,
        "patched": [],
        "submodels": [_serialize_submodel(package, sm, file_url) for sm in submodels],
    }


def documentation_needs_patch(package: AasxPackage) -> bool:
    submodels = find_documentation_submodels(package)
    if not submodels:
        return True
    return bool(_missing_main_slot_actions(submodels[0]))


def patch_documentation_slots(package: AasxPackage) -> dict[str, Any]:
    """Documentation Submodel과 메인 CAD/USD File 슬롯을 없으면 만든다."""
    submodel, created = _ensure_documentation_submodel(package)
    patched = _ensure_main_file_slots(submodel)
    return {
        "created": created,
        "patched": patched,
        "submodelId": submodel.id,
        "submodelIdShort": submodel.id_short,
    }


def apply_attachments(
    package: AasxPackage,
    attachments: Iterable[Attachment],
    store_twin: Optional[TwinFileStore] = None,
) -> dict[str, Any]:
    items = list(attachments)
    if not items:
        raise ValueError("첨부할 파일이 없습니다.")
    for item in items:
        if not item.content:
            raise ValueError(f"빈 파일은 첨부할 수 없습니다: {item.filename}")
        _validate_kind(item)
        _validate_reserved_name(item)

    submodel, created = _ensure_documentation_submodel(package)
    patched = _ensure_main_file_slots(submodel)
    added: list[dict[str, Any]] = []
    for item in items:
        added.append(_attach_one(package, submodel, item, store_twin))
    return {
        "created": created,
        "patched": patched,
        "submodelId": submodel.id,
        "submodelIdShort": submodel.id_short,
        "added": added,
    }


def _validate_kind(item: Attachment) -> None:
    ext = Path(item.filename).suffix.lower()
    if item.kind == "step" and ext not in _STEP_EXTS:
        raise ValueError("STEP 규격이 아닙니다. .stp 또는 .step 파일만 첨부할 수 있습니다.")
    if item.kind == "usd" and ext not in _USD_EXTS:
        raise ValueError("USD 규격이 아닙니다. .usd / .usda / .usdc / .usdz 파일만 첨부할 수 있습니다.")


def _validate_reserved_name(item: Attachment) -> None:
    slot = main_slot_for_name(item.id_short)
    if slot is not None and item.kind != slot.kind:
        raise ValueError(f"idShort '{slot.id_short}' 는 메인 {slot.display_ko} 전용입니다.")


def main_slot_for_name(id_short: str) -> Optional[MainFileSlot]:
    name = sanitize_id_short(id_short).lower()
    if not name:
        return None
    for slot in MAIN_FILE_SLOTS:
        if name in slot.names:
            return slot
    return None


def main_slot_for_semantic(semantic_id: str) -> Optional[MainFileSlot]:
    return MAIN_FILE_SEMANTIC_IDS.get((semantic_id or "").strip())


def main_slot_for_kind(kind: FileKind) -> Optional[MainFileSlot]:
    for slot in MAIN_FILE_SLOTS:
        if slot.kind == kind:
            return slot
    return None


def is_main_file_id_short(id_short: str) -> bool:
    return main_slot_for_name(id_short) is not None


def _missing_main_slot_actions(submodel: model.Submodel) -> list[str]:
    actions: list[str] = []
    for slot in MAIN_FILE_SLOTS:
        current = _find_slot_file(submodel, slot)
        if current is None:
            actions.append(slot.id_short)
        elif _slot_identity_drift(current, slot):
            actions.append(f"normalize:{current.id_short}->{slot.id_short}")
    return actions


def _find_slot_file(submodel: model.Submodel, slot: MainFileSlot) -> Optional[model.File]:
    by_semantic = None
    by_name = None
    for element in submodel.submodel_element:
        if not isinstance(element, model.File):
            continue
        if _semantic_text(element) == slot.semantic_id:
            by_semantic = element
        if (element.id_short or "").lower() in slot.names:
            by_name = element
    return by_semantic or by_name


def _slot_identity_drift(element: model.File, slot: MainFileSlot) -> bool:
    return element.id_short != slot.id_short or _semantic_text(element) != slot.semantic_id


def _ensure_main_file_slots(submodel: model.Submodel) -> list[str]:
    patched: list[str] = []
    for slot in MAIN_FILE_SLOTS:
        current = _find_slot_file(submodel, slot)
        if current is None:
            occupied = submodel.submodel_element.get("id_short", slot.id_short)
            if occupied is not None and not isinstance(occupied, model.File):
                continue
            submodel.submodel_element.add(_new_slot_file(slot))
            patched.append(slot.id_short)
            continue
        if not _slot_identity_drift(current, slot):
            continue
        _normalize_slot_file(submodel, current, slot)
        patched.append(slot.id_short)
    return patched


def _new_slot_file(slot: MainFileSlot, value: Optional[str] = None, content_type: str = "") -> model.File:
    return model.File(
        slot.id_short,
        content_type=content_type or slot.content_type,
        value=value,
        semantic_id=_semantic(slot.semantic_id),
        display_name=_slot_display_name(slot),
    )


def _normalize_slot_file(submodel: model.Submodel, current: model.File, slot: MainFileSlot) -> model.File:
    if current.id_short == slot.id_short:
        current.semantic_id = _semantic(slot.semantic_id)
        if not current.display_name:
            current.display_name = _slot_display_name(slot)
        if not current.content_type:
            current.content_type = slot.content_type
        return current
    submodel.submodel_element.remove(current)
    replacement = _new_slot_file(slot, current.value, current.content_type or slot.content_type)
    submodel.submodel_element.add(replacement)
    return replacement


def _slot_display_name(slot: MainFileSlot) -> model.MultiLanguageNameType:
    return model.MultiLanguageNameType({"ko": slot.display_ko, "en": slot.display_en})


def _ensure_documentation_submodel(package: AasxPackage) -> tuple[model.Submodel, bool]:
    existing = find_documentation_submodels(package)
    if existing:
        return existing[0], False

    shell = package.shells[0] if package.shells else None
    base_id = shell.id if shell is not None else f"https://camtic.or.kr/aas/{package.id}"
    submodel_id = f"{base_id.rstrip('/')}/sm/Documentation"
    suffix = 2
    while package.objects.get(submodel_id) is not None:
        submodel_id = f"{base_id.rstrip('/')}/sm/Documentation/{suffix}"
        suffix += 1

    submodel = model.Submodel(
        id_=submodel_id,
        id_short="Documentation",
        semantic_id=_semantic("0173-1#01-AHF578#003"),
        display_name=model.MultiLanguageNameType({"ko": "Documentation", "en": "Documentation"}),
        description=model.MultiLanguageTextType(
            {
                "ko": "DT 모델링 파일과 인수인계 문서를 첨부하는 Documentation Submodel",
                "en": "Documentation submodel for DT modeling files",
            }
        ),
    )
    package.objects.add(submodel)
    if shell is not None:
        shell.submodel.add(model.ModelReference.from_referable(submodel))
    return submodel, True


def _attach_one(
    package: AasxPackage,
    submodel: model.Submodel,
    item: Attachment,
    store_twin: Optional[TwinFileStore] = None,
) -> dict[str, Any]:
    part_name = package.files.add_file(
        _supplementary_name(item.filename),
        io.BytesIO(item.content),
        content_type_for(item.filename, item.content_type),
    )
    content_type = package.files.get_content_type(part_name)
    id_short, replaced = _upsert_file_element(submodel, item, part_name, content_type)
    disk_path = None
    slot = main_slot_for_name(id_short)
    if slot is not None and store_twin is not None:
        stored = store_twin(slot.kind, Path(item.filename).name, item.content)
        disk_path = str(stored) if stored else None
    return {
        "kind": item.kind,
        "idShort": id_short,
        "fileName": Path(item.filename).name,
        "packagePath": part_name,
        "contentType": content_type,
        "size": len(item.content),
        "replaced": replaced,
        "diskPath": disk_path,
    }


def _upsert_file_element(
    submodel: model.Submodel,
    item: Attachment,
    part_name: str,
    content_type: str,
) -> tuple[str, bool]:
    id_short = _resolve_id_short(submodel, item)
    slot = main_slot_for_name(id_short)
    existing = submodel.submodel_element.get("id_short", id_short)
    if isinstance(existing, model.File):
        existing.content_type = content_type
        existing.value = part_name
        existing.display_name = _slot_display_name(slot) if slot else _display_name(item, id_short)
        if slot is not None:
            existing.semantic_id = _semantic(slot.semantic_id)
        return id_short, True
    if existing is not None:
        raise ValueError(f"idShort '{id_short}' 는 이미 File이 아닌 항목입니다.")
    submodel.submodel_element.add(
        model.File(
            id_short,
            content_type=content_type,
            value=part_name,
            semantic_id=_semantic(slot.semantic_id) if slot else None,
            display_name=_slot_display_name(slot) if slot else _display_name(item, id_short),
        )
    )
    return id_short, False


def sanitize_id_short(raw: str) -> str:
    cleaned = _SAFE_ID_SHORT.sub("_", (raw or "").strip()).strip("_")
    return cleaned[:64]


def _resolve_id_short(submodel: model.Submodel, item: Attachment) -> str:
    preferred = sanitize_id_short(item.id_short)
    alias_slot = main_slot_for_name(preferred)
    if alias_slot is not None:
        return alias_slot.id_short
    if preferred and preferred[0].isalpha():
        return preferred
    slot = main_slot_for_kind(item.kind)
    if slot is not None:
        return slot.id_short
    used = {el.id_short for el in submodel.submodel_element if el.id_short}
    stem = sanitize_id_short(Path(item.filename).stem)
    if not stem or not stem[0].isalpha():
        stem = f"File_{stem}" if stem else "File"
    candidate = stem[:64]
    suffix = 2
    while candidate in used:
        candidate = f"{stem[:56]}_{suffix}"
        suffix += 1
    return candidate


def _display_name(item: Attachment, id_short: str) -> model.MultiLanguageNameType:
    label = id_short or Path(item.filename).name
    suffix = {"step": "STEP", "usd": "USD"}.get(item.kind)
    text = f"{label} ({suffix})" if suffix and suffix.lower() not in label.lower() else label
    return model.MultiLanguageNameType({"ko": text, "en": text})


def _serialize_submodel(
    package: AasxPackage, submodel: model.Submodel, file_url: FileUrlResolver
) -> dict[str, Any]:
    files = [
        _serialize_file(package, path, element, file_url)
        for path, element in _iter_files(submodel)
    ]
    return {
        "id": submodel.id,
        "idBase64": encode_id(submodel.id),
        "idShort": submodel.id_short,
        "semanticId": _semantic_text(submodel) or None,
        "files": _with_main_slots(files),
    }


def _serialize_file(
    package: AasxPackage,
    path: str,
    element: model.File,
    file_url: FileUrlResolver,
) -> dict[str, Any]:
    package_path = element.value or ""
    file_name = package_path.rsplit("/", 1)[-1] if package_path else None
    id_short = element.id_short or ""
    semantic_id = _semantic_text(element) or None
    slot = main_slot_for_semantic(semantic_id or "") or main_slot_for_name(id_short)
    return {
        "idShort": id_short,
        "path": path,
        "kind": _element_kind(element),
        "fileName": file_name,
        "contentType": element.content_type,
        "packagePath": package_path or None,
        "url": file_url(package_path) if package_path else None,
        "size": _file_size(package, package_path),
        "semanticId": semantic_id,
        "reserved": slot is not None,
        "missing": not package_path,
    }


def _virtual_slot(slot: MainFileSlot) -> dict[str, Any]:
    return {
        "idShort": slot.id_short,
        "path": slot.id_short,
        "kind": slot.kind,
        "fileName": None,
        "contentType": slot.content_type,
        "packagePath": None,
        "url": None,
        "size": None,
        "semanticId": slot.semantic_id,
        "reserved": True,
        "missing": True,
    }


def _slot_of_serialized(item: dict[str, Any]) -> Optional[MainFileSlot]:
    return main_slot_for_semantic(str(item.get("semanticId") or "")) or main_slot_for_name(
        str(item.get("idShort") or "")
    )


def _with_main_slots(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reserved: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for item in files:
        slot = _slot_of_serialized(item)
        if slot is not None:
            reserved[slot.id_short] = item
        else:
            item["reserved"] = False
            extras.append(item)
    ordered: list[dict[str, Any]] = []
    for slot in MAIN_FILE_SLOTS:
        item = reserved.get(slot.id_short) or _virtual_slot(slot)
        item["idShort"] = slot.id_short
        item["semanticId"] = slot.semantic_id
        item["reserved"] = True
        item["missing"] = not item.get("packagePath")
        ordered.append(item)
    ordered.extend(extras)
    return ordered


def _iter_files(submodel: model.Submodel) -> list[tuple[str, model.File]]:
    items: list[tuple[str, model.File]] = []

    def walk(element: model.SubmodelElement, path: str) -> None:
        if isinstance(element, model.File):
            items.append((path, element))
            return
        children: list[tuple[str, model.SubmodelElement]] = []
        if isinstance(element, model.SubmodelElementCollection):
            children = [(f"{path}.{child.id_short}", child) for child in element.value]
        elif isinstance(element, model.SubmodelElementList):
            children = [(f"{path}[{index}]", child) for index, child in enumerate(element.value)]
        elif isinstance(element, model.Entity):
            children = [(f"{path}.{child.id_short}", child) for child in element.statement]
        for child_path, child in children:
            walk(child, child_path)

    for element in submodel.submodel_element:
        walk(element, element.id_short or "")
    return items


def _element_kind(element: model.File) -> FileKind:
    slot = main_slot_for_semantic(_semantic_text(element)) or main_slot_for_name(element.id_short or "")
    if slot is not None:
        return slot.kind
    return kind_from_filename(element.value or element.id_short or "")


def _file_size(package: AasxPackage, name: Optional[str]) -> Optional[int]:
    if not name or name not in package.files:
        return None
    buffer = io.BytesIO()
    package.files.write_file(name, buffer)
    return len(buffer.getvalue())


def _supplementary_name(filename: str) -> str:
    name = Path(filename).name
    cleaned = _SAFE_FILENAME.sub("_", name).strip(" ._")
    if not cleaned:
        cleaned = "file.bin"
    return f"/aasx/suppl/{cleaned}"


def _semantic(value: str) -> model.ExternalReference:
    return model.ExternalReference((model.Key(model.KeyTypes.GLOBAL_REFERENCE, value),))


def _semantic_text(obj: model.HasSemantics) -> str:
    if obj.semantic_id is None or not obj.semantic_id.key:
        return ""
    return " / ".join(str(key.value) for key in obj.semantic_id.key)


def public_file_url(root_path: str, package_id: str, name: str) -> str:
    return f"{root_path}/api/packages/{quote(package_id, safe='')}/files/{quote(name.lstrip('/'))}"


def read_reserved_file(package: AasxPackage, slot: MainFileSlot) -> Optional[tuple[str, bytes]]:
    """메인 CAD/USD 슬롯에 첨부된 파일 바이트를 읽는다."""
    for submodel in find_documentation_submodels(package):
        element = _find_slot_file(submodel, slot)
        if element is None or not element.value or element.value not in package.files:
            continue
        content, _ = package.read_file(element.value)
        return Path(element.value).name, content
    return None


def read_extra_files(package: AasxPackage, suffixes: set[str]) -> list[tuple[str, bytes]]:
    """예약 슬롯이 아닌 Documentation 첨부 중 확장자가 일치하는 파일을 읽는다."""
    wanted = {item.lower() for item in suffixes}
    found: list[tuple[str, bytes]] = []
    for submodel in find_documentation_submodels(package):
        for _, element in _iter_files(submodel):
            if main_slot_for_semantic(_semantic_text(element)) or main_slot_for_name(element.id_short or ""):
                continue
            name = element.value or element.id_short or ""
            if Path(name).suffix.lower() not in wanted:
                continue
            if not element.value or element.value not in package.files:
                continue
            content, _ = package.read_file(element.value)
            found.append((Path(element.value).name, content))
    return found
