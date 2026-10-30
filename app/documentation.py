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
_STEP_ID_SHORTS = {"step", "stepmodel", "stepfile"}
_USD_ID_SHORTS = {"usd", "usdmodel", "usdfile"}
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
        "submodels": [_serialize_submodel(package, sm, file_url) for sm in submodels],
    }


def apply_attachments(package: AasxPackage, attachments: Iterable[Attachment]) -> dict[str, Any]:
    items = list(attachments)
    if not items:
        raise ValueError("첨부할 파일이 없습니다.")
    for item in items:
        if not item.content:
            raise ValueError(f"빈 파일은 첨부할 수 없습니다: {item.filename}")
        _validate_kind(item)

    submodel, created = _ensure_documentation_submodel(package)
    added: list[dict[str, Any]] = []
    for item in items:
        added.append(_attach_one(package, submodel, item))
    return {
        "created": created,
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


def _attach_one(package: AasxPackage, submodel: model.Submodel, item: Attachment) -> dict[str, Any]:
    part_name = package.files.add_file(
        _supplementary_name(item.filename),
        io.BytesIO(item.content),
        content_type_for(item.filename, item.content_type),
    )
    content_type = package.files.get_content_type(part_name)
    id_short, replaced = _upsert_file_element(submodel, item, part_name, content_type)
    return {
        "kind": item.kind,
        "idShort": id_short,
        "fileName": Path(item.filename).name,
        "packagePath": part_name,
        "contentType": content_type,
        "size": len(item.content),
        "replaced": replaced,
    }


def _upsert_file_element(
    submodel: model.Submodel,
    item: Attachment,
    part_name: str,
    content_type: str,
) -> tuple[str, bool]:
    id_short = _resolve_id_short(submodel, item)
    existing = submodel.submodel_element.get("id_short", id_short)
    if isinstance(existing, model.File):
        existing.content_type = content_type
        existing.value = part_name
        existing.display_name = _display_name(item, id_short)
        return id_short, True
    if existing is not None:
        raise ValueError(f"idShort '{id_short}' 는 이미 File이 아닌 항목입니다.")
    submodel.submodel_element.add(
        model.File(
            id_short,
            content_type=content_type,
            value=part_name,
            display_name=_display_name(item, id_short),
        )
    )
    return id_short, False


def sanitize_id_short(raw: str) -> str:
    cleaned = _SAFE_ID_SHORT.sub("_", (raw or "").strip()).strip("_")
    return cleaned[:64]


def _resolve_id_short(submodel: model.Submodel, item: Attachment) -> str:
    preferred = sanitize_id_short(item.id_short)
    if preferred and preferred[0].isalpha():
        return preferred
    used = {el.id_short for el in submodel.submodel_element if el.id_short}
    if item.kind == "step" and "STEP" not in used:
        return "STEP"
    if item.kind == "usd" and "USD" not in used:
        return "USD"
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
    return {
        "id": submodel.id,
        "idBase64": encode_id(submodel.id),
        "idShort": submodel.id_short,
        "semanticId": _semantic_text(submodel) or None,
        "files": [
            _serialize_file(package, path, element, file_url)
            for path, element in _iter_files(submodel)
        ],
    }


def _serialize_file(
    package: AasxPackage,
    path: str,
    element: model.File,
    file_url: FileUrlResolver,
) -> dict[str, Any]:
    package_path = element.value or ""
    file_name = package_path.rsplit("/", 1)[-1] if package_path else None
    return {
        "idShort": element.id_short,
        "path": path,
        "kind": _element_kind(element),
        "fileName": file_name,
        "contentType": element.content_type,
        "packagePath": package_path or None,
        "url": file_url(package_path) if package_path else None,
        "size": _file_size(package, package_path),
    }


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
    id_short = (element.id_short or "").lower()
    if id_short in _STEP_ID_SHORTS:
        return "step"
    if id_short in _USD_ID_SHORTS:
        return "usd"
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
