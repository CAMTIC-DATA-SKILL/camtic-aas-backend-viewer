"""Documentation STEP을 시뮬레이터와 같은 파트 분리 GLB로 변환한다.

camtic-digital-simulator/extract_step.py 와 같은 방법:
조립품 컴포넌트를 월드 좌표(m) 독립 Mesh로 두고, 노드 이름을
base_link / link1…link6 처럼 그대로 유지한다.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)

_STEP_EXTS = {".stp", ".step"}
# 기본 0.01은 수 MB가 되어 위젯 로딩이 느리다. 관절 인지에 충분한 해상도만 쓴다.
_TOL_LINEAR = 0.04
_TOL_ANGULAR = 0.6
_NAMED_LINKS = ("base_link", "link1", "link2", "link3", "link4", "link5", "link6")
_LINK_COLOR = (171, 195, 218, 255)
_TIP_COLOR = (71, 159, 217, 255)


def is_step_name(name: str) -> bool:
    return Path(name).suffix.lower() in _STEP_EXTS


def mesh_path_for(step_path: Path) -> Path:
    return step_path.with_suffix(".glb")


def glb_is_fresh(step_path: Path, glb_path: Path) -> bool:
    return (
        glb_path.is_file()
        and step_path.is_file()
        and glb_path.stat().st_mtime >= step_path.stat().st_mtime
        and glb_path.stat().st_size > 0
        and _is_flat_part_glb(glb_path)
    )


def glb_part_names(glb_path: Path) -> list[str]:
    """GLB JSON 청크에서 메시·노드 이름을 읽는다."""
    meta = _glb_json(glb_path)
    if not meta:
        return []
    names: list[str] = []
    for mesh in meta.get("meshes") or []:
        name = _canonical_part((mesh or {}).get("name") or "")
        if name and name not in names:
            names.append(name)
    if names:
        return _order_parts(names)
    for node in meta.get("nodes") or []:
        name = _canonical_part((node or {}).get("name") or "")
        if name and name not in names:
            names.append(name)
    return _order_parts(names)


def _canonical_part(raw: str) -> str:
    name = (raw or "").strip()
    if ":" in name and name.rsplit(":", 1)[-1].isdigit():
        name = name.rsplit(":", 1)[0]
    return name


def _order_parts(names: list[str]) -> list[str]:
    rank = {name: index for index, name in enumerate(_NAMED_LINKS)}
    named = [name for name in _NAMED_LINKS if name in names]
    extra = [name for name in names if name not in rank]
    return named + extra


def _glb_json(glb_path: Path) -> dict[str, Any]:
    try:
        data = glb_path.read_bytes()
    except OSError:
        return {}
    if len(data) < 20 or data[:4] != b"glTF":
        return {}
    chunk_len, chunk_type = struct.unpack_from("<II", data, 12)
    if chunk_type != 0x4E4F534A:
        return {}
    try:
        return json.loads(data[20 : 20 + chunk_len])
    except json.JSONDecodeError:
        return {}


def _is_flat_part_glb(glb_path: Path) -> bool:
    """extract_step 형식: 노드/메시 이름이 :N 접미사 없이 일치한다."""
    meta = _glb_json(glb_path)
    nodes = meta.get("nodes") or []
    meshes = meta.get("meshes") or []
    if not meshes:
        return False
    for node in nodes:
        raw = str((node or {}).get("name") or "")
        if ":" in raw and raw.rsplit(":", 1)[-1].isdigit():
            return False
    mesh_names = {(mesh or {}).get("name") for mesh in meshes}
    return bool(mesh_names and all(mesh_names))


def _flatten_assembly_glb(src: Path, dest: Path) -> list[str]:
    """cascadio 계층을 시뮬레이터처럼 월드 좌표 독립 Mesh로 편다."""
    import numpy as np
    import trimesh

    scene = trimesh.load(src, force="scene")
    out = trimesh.Scene()
    used: set[str] = set()
    parts: list[str] = []

    nodes = list(scene.graph.nodes_geometry)
    rank = {name: index for index, name in enumerate(_NAMED_LINKS)}

    def node_sort(node: str) -> tuple[int, int, str]:
        name = _canonical_part(node)
        if name in rank:
            return (0, rank[name], name)
        return (1, 0, name or node)

    for node in sorted(nodes, key=node_sort):
        transform, geom_name = scene.graph.get(node)
        geom = scene.geometry.get(geom_name)
        if geom is None:
            continue
        mesh = geom.copy()
        mesh.apply_transform(transform)
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.vertices) == 0:
            continue
        name = _canonical_part(node) or _canonical_part(str(geom_name)) or f"part{len(parts)}"
        unique = name
        suffix = 2
        while unique in used:
            unique = f"{name}_{suffix}"
            suffix += 1
        used.add(unique)
        mesh.remove_unreferenced_vertices()
        color = _TIP_COLOR if unique == "link6" else _LINK_COLOR
        mesh.visual.vertex_colors = np.array(color, dtype="uint8")
        out.add_geometry(mesh, geom_name=unique, node_name=unique)
        parts.append(unique)

    if not parts:
        raise RuntimeError("STEP 조립품에서 메시 파트를 찾지 못했습니다.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.export(dest)
    return _order_parts(parts)


def convert_step_to_glb(step_path: Path, glb_path: Path | None = None) -> dict[str, Any]:
    """조립품 파트를 extract_step와 같은 독립 Mesh GLB로 만든다."""
    try:
        import cascadio
    except ImportError as exc:
        raise RuntimeError("STEP 변환기(cascadio)가 설치되어 있지 않습니다.") from exc

    if not step_path.is_file():
        raise FileNotFoundError(str(step_path))
    dest = glb_path or mesh_path_for(step_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_name(f"{dest.stem}.cascadio.tmp.glb")
    try:
        cascadio.step_to_glb(
            str(step_path),
            str(raw),
            tol_linear=_TOL_LINEAR,
            tol_angular=_TOL_ANGULAR,
            merge_primitives=True,
        )
        if not raw.is_file() or raw.stat().st_size == 0:
            raise RuntimeError("STEP 변환 결과가 비어 있습니다.")
        parts = _flatten_assembly_glb(raw, dest)
    finally:
        raw.unlink(missing_ok=True)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError("STEP 파트 GLB를 만들지 못했습니다.")
    logger.info("Converted STEP %s → %s (%d bytes, %d parts)", step_path, dest, dest.stat().st_size, len(parts))
    return {"path": dest, "parts": parts, "bytes": dest.stat().st_size}


def ensure_step_glb(step_path: Path) -> dict[str, Any]:
    dest = mesh_path_for(step_path)
    if glb_is_fresh(step_path, dest):
        return {"path": dest, "parts": glb_part_names(dest), "bytes": dest.stat().st_size, "cached": True}
    info = convert_step_to_glb(step_path, dest)
    info["cached"] = False
    return info
