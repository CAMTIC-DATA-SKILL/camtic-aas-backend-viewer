"""패키지별 로봇 시뮬레이션 페이로드 — AAS STEP + 선택 URDF."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from basyx.aas import model

from .documentation import (
    MAIN_CAD_SLOT,
    find_documentation_submodels,
    read_extra_files,
    read_reserved_file,
)
from .repository import AasxPackage, AasxRepository
from .step_mesh import ensure_step_glb, is_step_name
from .urdf import read_urdf
from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)

_KINEMATIC_SHORTS = {"robotkinematicstate", "robotkinematics"}
_SKIP_COLLECTION = re.compile(
    r"speed|velocity|accel|torque|target|command|desired|setpoint|offset|limit|force",
    re.I,
)
_JOINT_LEAF = re.compile(r"^j([1-6])$", re.I)
_CART_LEAF = {
    "x": "X",
    "y": "Y",
    "z": "Z",
    "rx": "Rx",
    "ry": "Ry",
    "rz": "Rz",
    "component1": "Rx",
    "component2": "Ry",
    "component3": "Rz",
}
_JOINT_COLLECTIONS = ("jointposition", "jointangles", "actualjointpositions", "jointstate")
_TCP_COLLECTIONS = ("tcpposition", "actualposition", "cartesianposition", "cartesian")
_ORI_COLLECTIONS = ("tcporientation", "actualorientation", "orientation", "rotation")
_NAMED_LINKS = ("base_link", "link1", "link2", "link3", "link4", "link5", "link6")
# 이름이 표준 6축 링크일 때만 쓰는 추정 피벗. URDF가 있으면 이쪽은 쓰지 않는다.
_SUGGESTED_JOINTS = (
    {"name": "J1", "pivot": [0.0, 0.0, 0.223], "axis": [0.0, 0.0, 1.0]},
    {"name": "J2", "pivot": [0.0, 0.0, 0.446], "axis": [0.0, -1.0, 0.0]},
    {"name": "J3", "pivot": [0.0, 0.0, 0.89], "axis": [0.0, -1.0, 0.0]},
    {"name": "J4", "pivot": [0.10, 0.0, 0.93], "axis": [1.0, 0.0, 0.0]},
    {"name": "J5", "pivot": [0.51, 0.0, 0.93], "axis": [0.0, -1.0, 0.0]},
    {"name": "J6", "pivot": [0.5906, 0.0, 0.93], "axis": [1.0, 0.0, 0.0]},
)


def has_robot_kinematics(package: AasxPackage) -> bool:
    return _kinematic_submodel(package) is not None


def _kinematic_submodel(package: AasxPackage) -> Optional[model.Submodel]:
    for submodel in package.submodels:
        if (submodel.id_short or "").lower() in _KINEMATIC_SHORTS:
            return submodel
    return None


def _iter_submodel_properties(
    element: model.SubmodelElement, path: str
) -> list[tuple[str, model.Property]]:
    if isinstance(element, model.Property):
        return [(path, element)] if path else []
    children: list[tuple[str, model.SubmodelElement]] = []
    if isinstance(element, model.SubmodelElementCollection):
        children = [(f"{path}.{child.id_short}" if path else child.id_short or "", child) for child in element.value]
    elif isinstance(element, model.SubmodelElementList):
        children = [(f"{path}[{index}]", child) for index, child in enumerate(element.value)]
    elif isinstance(element, model.Entity):
        children = [
            (f"{path}.{child.id_short}" if path else child.id_short or "", child) for child in element.statement
        ]
    items: list[tuple[str, model.Property]] = []
    for child_path, child in children:
        items.extend(_iter_submodel_properties(child, child_path))
    return items


def _axis_for_property(path: str) -> Optional[tuple[str, int]]:
    parts = [part for part in path.replace("/", ".").split(".") if part]
    if not parts:
        return None
    collection = "".join(part for part in parts[:-1]).lower()
    if _SKIP_COLLECTION.search(collection):
        return None
    leaf = re.sub(r"[^a-z0-9]", "", parts[-1].lower())
    joint = _JOINT_LEAF.match(leaf)
    if joint:
        score = 80 if any(name in collection for name in _JOINT_COLLECTIONS) else 20
        return f"J{joint.group(1)}", score
    cart = _CART_LEAF.get(leaf)
    if not cart:
        return None
    if cart in {"Rx", "Ry", "Rz"}:
        if leaf.startswith("component") and not any(name in collection for name in _ORI_COLLECTIONS):
            return None
        score = 80 if any(name in collection for name in _ORI_COLLECTIONS) else 25
        return cart, score
    score = 80 if any(name in collection for name in _TCP_COLLECTIONS) else 25
    return cart, score


def axis_bindings_from_package(package: AasxPackage) -> dict[str, Any]:
    """RobotKinematicState Submodel의 하위 Property를 J1~J6 / TCP 축에 묶는다."""
    submodel = _kinematic_submodel(package)
    if submodel is None:
        return {"submodelIdShort": None, "bindings": {}, "candidates": []}

    candidates: list[dict[str, Any]] = []
    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for path, element in (
        item
        for child in submodel.submodel_element
        for item in _iter_submodel_properties(child, child.id_short or "")
    ):
        hit = _axis_for_property(path)
        record = {
            "submodelIdShort": submodel.id_short,
            "path": path,
            "idShort": element.id_short,
            "key": f"{submodel.id_short}/{path}",
            "axis": hit[0] if hit else None,
        }
        candidates.append(record)
        if hit is None:
            continue
        axis, score = hit
        current = best.get(axis)
        if current is None or score > current[0]:
            best[axis] = (score, record)

    bindings = {
        axis: {
            "axis": axis,
            "submodelIdShort": item["submodelIdShort"],
            "path": item["path"],
            "idShort": item["idShort"],
            "key": item["key"],
        }
        for axis, (_score, item) in best.items()
    }
    return {
        "submodelIdShort": submodel.id_short,
        "bindings": bindings,
        "candidates": candidates,
    }


def simulation_mesh_url(root_path: str, package_id: str) -> str:
    return f"{root_path}/api/packages/{quote(package_id, safe='')}/simulation/mesh"


def ensure_step_on_disk(repository: AasxRepository, package: AasxPackage) -> Optional[Path]:
    existing = repository.find_digital_twin_file(package.id, "step")
    if existing is not None:
        return existing
    reserved = read_reserved_file(package, MAIN_CAD_SLOT)
    if reserved is None:
        return None
    filename, content = reserved
    if not is_step_name(filename):
        return None
    return repository.write_digital_twin_file(package.id, "step", filename, content)


def ensure_package_mesh(repository: AasxRepository, package: AasxPackage) -> dict[str, Any]:
    step_path = ensure_step_on_disk(repository, package)
    if step_path is None:
        return {"stepPath": None, "meshPath": None, "parts": [], "bytes": 0, "error": None}
    try:
        info = ensure_step_glb(step_path)
    except Exception as exc:
        logger.warning("STEP mesh conversion failed for %s: %s", package.id, exc)
        return {
            "stepPath": step_path,
            "meshPath": None,
            "parts": [],
            "bytes": 0,
            "error": f"STEP 메시 변환 실패: {exc}",
        }
    return {
        "stepPath": step_path,
        "meshPath": info["path"],
        "parts": info["parts"],
        "bytes": info["bytes"],
        "error": None,
    }


def _joint_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name") or "",
        "type": item.get("type") or "revolute",
        "parent": item.get("parent") or "",
        "child": item.get("child") or "",
        "pivot": [float(v) for v in item.get("pivot") or [0, 0, 0]],
        "axis": [float(v) for v in item.get("axis") or [1, 0, 0]],
        "zeroQuaternion": [float(v) for v in item.get("zeroQuaternion") or [0, 0, 0, 1]],
        "lower": float(item.get("lower", -math.pi)),
        "upper": float(item.get("upper", math.pi)),
    }


def _suggested_joints() -> list[dict[str, Any]]:
    return [
        _joint_payload(
            {
                **item,
                "type": "revolute",
                "parent": "base_link" if item["name"] == "J1" else f"link{int(item['name'][1]) - 1}",
                "child": f"link{item['name'][1]}",
                "zeroQuaternion": [0, 0, 0, 1],
                "lower": -math.pi,
                "upper": math.pi,
            }
        )
        for item in _SUGGESTED_JOINTS
    ]


def _urdf_from_package(package: AasxPackage) -> Optional[tuple[str, bytes]]:
    extras = read_extra_files(package, {".urdf"})
    return extras[0] if extras else None


def build_simulation_payload(
    package: AasxPackage,
    repository: AasxRepository,
    root_path: str = "",
) -> dict[str, Any]:
    kinematics = has_robot_kinematics(package)
    mesh = ensure_package_mesh(repository, package)
    parts = list(mesh.get("parts") or [])
    named = all(name in parts for name in _NAMED_LINKS)

    urdf_info = None
    joints: list[dict[str, Any]] = []
    source = None
    warnings: list[str] = []
    urdf_file = _urdf_from_package(package)
    if urdf_file is not None:
        try:
            parsed = read_urdf(urdf_file[1].decode("utf-8-sig"))
            movable = [
                item
                for item in parsed["joints"]
                if item["type"] in ("revolute", "continuous", "prismatic")
            ]
            joints = [_joint_payload(item) for item in movable[:6]]
            source = "urdf"
            urdf_info = {
                "fileName": urdf_file[0],
                "robot": parsed.get("robot"),
                "baseLinks": parsed.get("base_links") or [],
            }
            warnings.extend(parsed.get("warnings") or [])
            if len(joints) < 6:
                warnings.append(f"URDF 가동 관절이 {len(joints)}개입니다. 6축 구동은 제한됩니다.")
        except Exception as exc:
            warnings.append(f"URDF 해석 실패: {exc}")

    if not joints and named:
        joints = _suggested_joints()
        source = "suggested"
        warnings.append("Documentation에 URDF가 없어 링크 이름 기준 추정 관절축을 사용합니다.")

    step_path = mesh.get("stepPath")
    mesh_path = mesh.get("meshPath")
    axes = axis_bindings_from_package(package)
    if kinematics and not axes["bindings"]:
        warnings.append("RobotKinematicState Submodel에서 J1~J6 / TCP Property를 찾지 못했습니다.")
    return {
        "packageId": package.id,
        "hasKinematics": kinematics,
        "hasMesh": mesh_path is not None,
        "articulated": bool(joints) and named,
        "cadFile": step_path.name if isinstance(step_path, Path) else None,
        "meshUrl": simulation_mesh_url(root_path, package.id) if mesh_path else None,
        "meshBytes": mesh.get("bytes") or 0,
        "parts": parts,
        "joints": joints,
        "kinematicsSource": source,
        "urdf": urdf_info,
        "axisSubmodel": axes["submodelIdShort"],
        "axisBindings": axes["bindings"],
        "axisCandidates": axes["candidates"],
        "driveDefault": "tcp",
        "warnings": warnings,
        "error": mesh.get("error"),
        "documentationSubmodels": [sm.id_short for sm in find_documentation_submodels(package)],
    }
