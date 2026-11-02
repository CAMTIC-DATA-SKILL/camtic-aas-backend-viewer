"""AASX 패키지의 노드·AAS 경로 해석 (DB 등록 기능 없음).

새 매핑을 만들지 않는다. 기존 OPC-UA 구독이 쓰는 것과 같은 engineering.csv 레코드
(`_engineering_live_nodes`: RobotKinematicState / RobotOperationalState)에서 노드와 AAS 경로를 그대로 가져온다.
NodeId 는 구독과 똑같이 카탈로그의 nodeIdNamespace 로 바꾼 값(예: ns=2;i=1145)을 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


from ..opcua_api import _engineering_live_nodes
from ..opcua_export import load_engineering_nodes
from ..opcua_mapping import rewrite_node_id, split_engineering_path
from ..repository import AasxPackage, AasxRepository

# 구독 29개 중 저장 대상에서 빼는 경로 (공구 TCP 오프셋은 설정값이라 시계열로 쌓지 않는다) → 23개
EXCLUDED_PREFIXES = ("ToolPoseTCPOffset",)


@dataclass(frozen=True)
class AasNode:
    node_id: str           # 현장 NodeId (네임스페이스 변환 후)
    node_name: str         # 논리 이름 = AAS 경로 (예: JointPosition.J1)
    data_type: str
    unit: Optional[str]
    submodel_id_short: str
    submodel_id: str
    path: str              # Submodel 안 idShort 경로 (뷰어 트리의 path 와 같음)


def _unit_rules(records: list[dict[str, Any]], package_id: str) -> list[tuple[str, str]]:
    """DataAcquisitionConfiguration/Mappings/<Map>/{TargetAASPath, Unit} → [(경로 접두사 또는 경로, 단위)].

    TargetAASPath 예: RobotKinematicState/JointPosition/J1..J6 · RobotKinematicState/TCPPosition/X..Z ·
    RobotKinematicState/TCPSpeed. 마지막 마디에 '..' 이나 '*' 가 있으면 그 앞까지를 접두사로 본다.
    Unit 이 TBD / - / 빈 값이면 단위 없음.
    """
    by_map: dict[str, dict[str, str]] = {}
    for record in records:
        parts = [p for p in str(record.get("aasPath") or "").split("/") if p]
        if "Mappings" not in parts or len(parts) < 2 or parts[-1] not in ("TargetAASPath", "Unit"):
            continue
        by_map.setdefault("/".join(parts[:-1]), {})[parts[-1]] = str(record.get("templateValue") or "").strip()
    rules = []
    for item in by_map.values():
        target, unit = item.get("TargetAASPath", ""), item.get("Unit", "").strip('"')
        if not target or unit in ("", "-", "TBD"):
            continue
        head, _, last = target.rpartition("/")
        rules.append((f"{head}/" if (".." in last or "*" in last) else target, unit))
    return rules


def _unit_for(rules: list[tuple[str, str]], submodel: str, path: str) -> Optional[str]:
    full = f"{submodel}/{path.replace('.', '/')}"
    for target, unit in rules:
        if full == target or (target.endswith("/") and full.startswith(target)):
            return unit
    return None


def package_nodes(package: AasxPackage, repository: AasxRepository, namespace: Optional[int]) -> list[AasNode]:
    submodel_ids = {str(sm.id_short): str(sm.id) for sm in package.submodels}
    all_records = load_engineering_nodes(repository.artifact_dir(package.id), variables_only=True)
    records = _engineering_live_nodes(all_records, package.id)
    rules = _unit_rules(all_records, package.id)
    nodes: list[AasNode] = []
    for record in records:
        submodel, path = split_engineering_path(str(record.get("aasPath") or ""), package.id)
        if not path or path.startswith(EXCLUDED_PREFIXES) or submodel not in submodel_ids:
            continue
        nodes.append(AasNode(
            node_id=rewrite_node_id(str(record["nodeId"]), namespace),
            node_name=path,
            data_type=str(record.get("dataType") or "String"),
            unit=_unit_for(rules, submodel, path),
            submodel_id_short=submodel,
            submodel_id=submodel_ids[submodel],
            path=path,
        ))
    return nodes

