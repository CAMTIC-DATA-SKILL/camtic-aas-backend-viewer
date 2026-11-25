"""AASX 템플릿 아카이브를 읽어 설비 메타를 올리고, 미들웨어 값이 TSDB 에 들어가게 한다.

검사 대상은 ``packages/{id}/opcua/engineering.csv`` 다. 교체 보관용 archive 폴더는 읽지 않는다.
카탈로그 ``aasIds`` 에 있는 셸만 올린다. 비어 있으면 산출물이 있는 패키지를 모두 올린다.

등록하는 신호:
- DataAcquisitionConfiguration 매핑의 StorageTarget 이 TSDB 이면 mapping_type TIME_SERIES
- StorageTarget 이 EventDB 이면 mapping_type STATUS (값 자체는 같은 opcua_sample 에 쌓인다)
- PLC IOInterfaceMapping 의 AASPath 가 가리키는 신호는 TIME_SERIES

equipment / equipment_node / aas_node_mapping 만 UPSERT 한다. 샘플 행과 기존 행은 지우지 않고,
운영자가 끈 enabled 도 다시 켜지 않는다. 스키마는 바꾸지 않는다.
같은 셸 id 가 이미 equipment.aas_id 에 있으면 그 equipment_id 를 그대로 쓴다.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from utils.log_setup import LogSetup

from ..opcua_export import load_engineering_nodes
from ..opcua_mapping import rewrite_node_id, split_engineering_path
from .models import AasNodeMapping, Equipment, EquipmentNode

logger = LogSetup.get_logger(__name__)

_EQUIPMENT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DATA_TYPES = frozenset({
    "Boolean", "SByte", "Byte", "Int16", "UInt16", "Int32", "UInt32",
    "Int64", "UInt64", "Float", "Double", "String", "DateTime",
})
_TYPE_ALIASES = {
    "bool": "Boolean", "boolean": "Boolean", "xs:boolean": "Boolean",
    "sbyte": "SByte", "byte": "Byte",
    "int16": "Int16", "uint16": "UInt16",
    "int32": "Int32", "uint32": "UInt32", "int": "Int32", "dint": "Int32", "xs:int": "Int32",
    "int64": "Int64", "uint64": "UInt64",
    "float": "Float", "real": "Float", "xs:float": "Float",
    "double": "Double", "lreal": "Double", "xs:double": "Double",
    "string": "String", "xs:string": "String",
    "datetime": "DateTime", "xs:datetime": "DateTime",
}
_STORAGE_TYPE = {"TSDB": "TIME_SERIES", "EVENTDB": "STATUS", "EVENT_DB": "STATUS"}
_TYPE_RANK = {"TIME_SERIES": 2, "STATUS": 1, "CURRENT_VALUE": 0}
_BLANK = frozenset({"", "-", "TBD", "N/A", "NA"})


@dataclass(frozen=True)
class SyncContext:
    endpoint: str
    namespace: Optional[int]
    aas_ids: frozenset[str]


@dataclass(frozen=True)
class _Rule:
    target: str
    mapping_type: str
    unit: Optional[str]
    sampling_ms: int
    exact: bool

    @property
    def specificity(self) -> tuple[int, int]:
        return (1 if self.exact else 0, len(self.target))


@dataclass(frozen=True)
class NodePlan:
    node_id: str
    node_name: str
    data_type: str
    unit: Optional[str]
    sampling_ms: int
    submodel_id: str
    path: str
    mapping_type: str


@dataclass(frozen=True)
class EquipmentPlan:
    package_id: str
    equipment_id: str
    equipment_type: str
    equipment_name: str
    manufacturer: Optional[str]
    model_name: Optional[str]
    serial_number: Optional[str]
    aas_id: str
    opcua_endpoint: str
    nodes: tuple[NodePlan, ...]


def equipment_id_for(package_id: str) -> str:
    """equipment_id 제약 ``^[A-Za-z0-9_.-]{1,64}$`` 에 맞게 패키지 폴더 이름을 줄인다."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", (package_id or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_.-")
    cleaned = (cleaned[:64] if cleaned else "").strip("_.-")
    if not cleaned or not _EQUIPMENT_ID.fullmatch(cleaned):
        return ""
    return cleaned


def _meaningful(value: Any) -> Optional[str]:
    text = str(value or "").strip().strip('"')
    if text.upper() in _BLANK:
        return None
    return text


def _data_type(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if text in _DATA_TYPES:
        return text
    return _TYPE_ALIASES.get(text.lower())


def _sampling_ms(raw: Any) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 100
    return value if value > 0 else 100


def _full_path(submodel: str, path: str) -> str:
    if not path:
        return submodel
    return f"{submodel}/{path.replace('.', '/')}"


def _rule_matches(rule: _Rule, full: str) -> bool:
    if rule.exact:
        return full == rule.target
    prefix = rule.target
    return full.startswith(prefix) and full != prefix.rstrip("/")


def _submodel_ids(records: Sequence[dict[str, Any]], package_id: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for record in records:
        if record.get("modelType") != "Submodel" or not record.get("aasId"):
            continue
        submodel, path = split_engineering_path(str(record.get("aasPath") or ""), package_id)
        if submodel and not path:
            found[submodel] = str(record["aasId"])
    return found


def _shell(records: Sequence[dict[str, Any]], allowed: frozenset[str]) -> Optional[dict[str, Any]]:
    shells = [record for record in records if record.get("modelType") == "AssetAdministrationShell" and record.get("aasId")]
    if allowed:
        shells = [record for record in shells if str(record["aasId"]) in allowed]
    return shells[0] if shells else None


def _ident(records: Sequence[dict[str, Any]], package_id: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for record in records:
        if record.get("nodeClass") != "Variable":
            continue
        submodel, path = split_engineering_path(str(record.get("aasPath") or ""), package_id)
        if submodel == "Identification" and path in {"ManufacturerName", "ProductDesignation", "ManufacturerTypName",
                                                      "SerialNumber", "AssetType"}:
            text = _meaningful(record.get("templateValue"))
            if text and path not in values:
                values[path] = text
    return values


def _equipment_type(asset_type: Optional[str], package_id: str, shell_id: str, display: str,
                    submodels: set[str]) -> str:
    blob = " ".join((asset_type or "", package_id, shell_id, display)).lower()
    if "plc" in blob:
        return "PLC"
    if any(token in blob for token in ("amr", "agv", "scout")):
        return "AMR"
    if "sensor" in blob and "robot" not in blob:
        return "SENSOR"
    if "robot" in blob or "RobotKinematicState" in submodels:
        return "ROBOT"
    if "OperationalData" in submodels:
        return "AMR"
    return "MACHINE"


def _acquisition_rules(records: Sequence[dict[str, Any]], package_id: str) -> list[_Rule]:
    groups: dict[str, dict[str, str]] = {}
    for record in records:
        if record.get("nodeClass") != "Variable":
            continue
        submodel, path = split_engineering_path(str(record.get("aasPath") or ""), package_id)
        if submodel != "DataAcquisitionConfiguration" or ".Mappings." not in f".{path}.":
            continue
        leaf = path.rsplit(".", 1)[-1]
        if leaf not in {"TargetAASPath", "StorageTarget", "Unit", "SamplingIntervalMs"}:
            continue
        groups.setdefault(path.rsplit(".", 1)[0], {})[leaf] = str(record.get("templateValue") or "")
    rules: list[_Rule] = []
    for item in groups.values():
        target = str(item.get("TargetAASPath") or "").strip().strip("/")
        storage = str(item.get("StorageTarget") or "").strip().upper().replace(" ", "")
        mapping_type = _STORAGE_TYPE.get(storage)
        if not target or mapping_type is None:
            continue
        head, _, last = target.rpartition("/")
        exact = ".." not in last and "*" not in last
        prefix = f"{head}/" if head and not exact else target
        rules.append(_Rule(prefix if not exact else target, mapping_type, _meaningful(item.get("Unit")),
                           _sampling_ms(item.get("SamplingIntervalMs")), exact))
    return rules


def _matches_io(targets: set[str], full: str) -> bool:
    for target in targets:
        head, _, last = target.rpartition("/")
        if ".." in last or "*" in last:
            prefix = f"{head}/" if head else target
            if full.startswith(prefix) and full != head:
                return True
        elif full == target:
            return True
    return False


def _io_targets(records: Sequence[dict[str, Any]], package_id: str) -> set[str]:
    targets: set[str] = set()
    for record in records:
        if record.get("nodeClass") != "Variable":
            continue
        submodel, path = split_engineering_path(str(record.get("aasPath") or ""), package_id)
        if submodel != "IOInterfaceMapping" or path.rsplit(".", 1)[-1] != "AASPath":
            continue
        target = str(record.get("templateValue") or "").strip().strip("/")
        if target:
            targets.add(target)
    return targets


def _best_rule(rules: Sequence[_Rule], full: str) -> Optional[_Rule]:
    found: Optional[_Rule] = None
    for rule in rules:
        if not _rule_matches(rule, full):
            continue
        if found is None or (_TYPE_RANK[rule.mapping_type], rule.specificity) > (
            _TYPE_RANK[found.mapping_type], found.specificity
        ):
            found = rule
    return found


def plan_from_records(
    package_id: str,
    records: Sequence[dict[str, Any]],
    *,
    namespace: Optional[int],
    endpoint: str,
    allowed_aas_ids: frozenset[str] = frozenset(),
) -> Optional[EquipmentPlan]:
    """engineering.csv 레코드로 설비 계획 하나를 만든다. 카탈로그에 없으면 None."""
    if not endpoint or not records:
        return None
    shell = _shell(records, allowed_aas_ids)
    if shell is None:
        return None
    equipment_id = equipment_id_for(package_id)
    aas_id = str(shell["aasId"])
    if not equipment_id or not aas_id:
        logger.warning("패키지 %s 의 equipment_id 를 만들지 못해 건너뜁니다.", package_id)
        return None
    submodels = _submodel_ids(records, package_id)
    ident = _ident(records, package_id)
    display = _meaningful(shell.get("displayName")) or equipment_id
    rules = _acquisition_rules(records, package_id)
    io_targets = _io_targets(records, package_id)
    nodes: dict[str, NodePlan] = {}
    for record in records:
        if record.get("nodeClass") != "Variable" or not record.get("nodeId"):
            continue
        submodel, path = split_engineering_path(str(record.get("aasPath") or ""), package_id)
        if not submodel or not path or submodel not in submodels:
            continue
        full = _full_path(submodel, path)
        rule = _best_rule(rules, full)
        if rule is None and _matches_io(io_targets, full):
            rule = _Rule(full, "TIME_SERIES", None, 100, True)
        if rule is None:
            continue
        data_type = _data_type(record.get("dataType"))
        if data_type is None:
            logger.warning("저장 제외(데이터 타입): %s %s", package_id, full)
            continue
        node_id = rewrite_node_id(str(record["nodeId"]), namespace)
        plan = NodePlan(
            node_id=node_id,
            node_name=path,
            data_type=data_type,
            unit=rule.unit,
            sampling_ms=rule.sampling_ms,
            submodel_id=submodels[submodel],
            path=path,
            mapping_type=rule.mapping_type,
        )
        current = nodes.get(node_id)
        if current is None or _TYPE_RANK[plan.mapping_type] > _TYPE_RANK[current.mapping_type]:
            nodes[node_id] = plan
    deduped: dict[tuple[str, str, str], NodePlan] = {}
    for node in nodes.values():
        deduped.setdefault((node.submodel_id, node.path, node.mapping_type), node)
    ordered = tuple(sorted(deduped.values(), key=lambda node: (node.path, node.node_id)))
    model_name = ident.get("ProductDesignation") or ident.get("ManufacturerTypName")
    return EquipmentPlan(
        package_id=package_id,
        equipment_id=equipment_id,
        equipment_type=_equipment_type(ident.get("AssetType"), package_id, aas_id, display, set(submodels)),
        equipment_name=display,
        manufacturer=ident.get("ManufacturerName"),
        model_name=model_name,
        serial_number=ident.get("SerialNumber"),
        aas_id=aas_id,
        opcua_endpoint=endpoint,
        nodes=ordered,
    )


def discover_plans(
    packages_dir: Path,
    opcua_dirname: str,
    *,
    namespace: Optional[int],
    endpoint: str,
    allowed_aas_ids: frozenset[str] = frozenset(),
) -> list[EquipmentPlan]:
    if not packages_dir.is_dir():
        logger.warning("AASX 패키지 폴더가 없습니다: %s", packages_dir)
        return []
    plans: list[EquipmentPlan] = []
    for package_dir in sorted(path for path in packages_dir.iterdir() if path.is_dir()):
        records = load_engineering_nodes(package_dir / opcua_dirname, variables_only=False)
        plan = plan_from_records(
            package_dir.name, records, namespace=namespace, endpoint=endpoint, allowed_aas_ids=allowed_aas_ids,
        )
        if plan is not None:
            plans.append(plan)
    return plans


def resolve_equipment_ids(
    plans: Sequence[EquipmentPlan],
    existing: Sequence[tuple[str, Optional[str]]],
) -> list[EquipmentPlan]:
    """이미 같은 aas_id 로 등록된 설비가 있으면 그 equipment_id 를 유지한다."""
    by_aas: dict[str, str] = {}
    for equipment_id, aas_id in existing:
        if aas_id and aas_id not in by_aas:
            by_aas[aas_id] = equipment_id
    resolved: list[EquipmentPlan] = []
    for plan in plans:
        equipment_id = by_aas.get(plan.aas_id, plan.equipment_id)
        if not _EQUIPMENT_ID.fullmatch(equipment_id):
            logger.warning("equipment_id 형식이 아니어서 건너뜁니다: %s", equipment_id)
            continue
        resolved.append(plan if equipment_id == plan.equipment_id else replace(plan, equipment_id=equipment_id))
    return resolved


def plan_token(plans: Sequence[EquipmentPlan]) -> tuple:
    items = []
    for plan in sorted(plans, key=lambda item: item.equipment_id):
        nodes = tuple(sorted(
            (node.node_id, node.node_name, node.data_type, node.unit, node.sampling_ms,
             node.submodel_id, node.path, node.mapping_type)
            for node in plan.nodes
        ))
        items.append((
            plan.equipment_id, plan.equipment_type, plan.equipment_name, plan.manufacturer,
            plan.model_name, plan.serial_number, plan.aas_id, plan.opcua_endpoint, nodes,
        ))
    return tuple(items)


async def apply_plans(session: AsyncSession, plans: Sequence[EquipmentPlan]) -> tuple[int, int]:
    """설비·노드·매핑을 UPSERT 한다. 커밋은 호출자가 한다. 반환은 (노드 수, 매핑 수)."""
    node_count = 0
    mapping_count = 0
    for plan in plans:
        equipment = insert(Equipment).values(
            equipment_id=plan.equipment_id,
            equipment_type=plan.equipment_type,
            equipment_name=plan.equipment_name,
            manufacturer=plan.manufacturer,
            model_name=plan.model_name,
            serial_number=plan.serial_number,
            aas_id=plan.aas_id,
            opcua_endpoint=plan.opcua_endpoint,
            data_source="REAL",
            enabled=True,
        )
        excluded = equipment.excluded
        await session.execute(equipment.on_conflict_do_update(
            index_elements=["equipment_id"],
            set_={
                "equipment_type": excluded.equipment_type,
                "equipment_name": excluded.equipment_name,
                "manufacturer": excluded.manufacturer,
                "model_name": excluded.model_name,
                "serial_number": excluded.serial_number,
                "aas_id": excluded.aas_id,
                "opcua_endpoint": excluded.opcua_endpoint,
            },
        ))
        if not plan.nodes:
            continue
        node_stmt = insert(EquipmentNode).values([
            {
                "equipment_id": plan.equipment_id,
                "node_id": node.node_id,
                "node_name": node.node_name,
                "data_type": node.data_type,
                "unit": node.unit,
                "sampling_ms": node.sampling_ms,
                "enabled": True,
            }
            for node in plan.nodes
        ])
        node_excluded = node_stmt.excluded
        await session.execute(node_stmt.on_conflict_do_update(
            index_elements=["equipment_id", "node_id"],
            set_={
                "node_name": node_excluded.node_name,
                "data_type": node_excluded.data_type,
                "unit": node_excluded.unit,
                "sampling_ms": node_excluded.sampling_ms,
            },
        ))
        map_stmt = insert(AasNodeMapping).values([
            {
                "equipment_id": plan.equipment_id,
                "node_id": node.node_id,
                "aas_submodel_id": node.submodel_id,
                "aas_id_short_path": node.path,
                "mapping_type": node.mapping_type,
            }
            for node in plan.nodes
        ])
        await session.execute(map_stmt.on_conflict_do_update(
            index_elements=["equipment_id", "aas_submodel_id", "aas_id_short_path", "mapping_type"],
            set_={"node_id": map_stmt.excluded.node_id},
        ))
        node_count += len(plan.nodes)
        mapping_count += len(plan.nodes)
    return node_count, mapping_count


class PackageSync:
    """DB 가 연결된 동안 패키지 폴더를 주기적으로 검사한다."""

    def __init__(
        self,
        runtime: Any,
        packages_dir: Path,
        opcua_dirname: str,
        context: Callable[[], Optional[SyncContext]],
    ) -> None:
        self.runtime = runtime
        self.packages_dir = packages_dir
        self.opcua_dirname = opcua_dirname
        self._context = context
        self.interval = max(5.0, float(runtime.cfg.sync_interval_s))
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._token: Optional[tuple] = None
        self.last_scan_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.packages = 0
        self.nodes = 0
        self.mappings = 0
        self.unchanged = False

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="aasx-package-sync")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self._safe_sync()
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue
            return

    async def _safe_sync(self) -> None:
        try:
            await self.sync_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = type(exc).__name__
            logger.warning("AASX 패키지 메타 동기화 실패: %s", type(exc).__name__)

    async def sync_once(self) -> None:
        context = self._context()
        if context is None or not context.endpoint:
            if self.last_error != "no_endpoint":
                logger.warning("패키지 동기화에 쓸 OPC-UA 엔드포인트가 없습니다.")
            self.last_error = "no_endpoint"
            return
        plans = discover_plans(
            self.packages_dir,
            self.opcua_dirname,
            namespace=context.namespace,
            endpoint=context.endpoint,
            allowed_aas_ids=context.aas_ids,
        )
        async with self.runtime.sessions() as session:
            rows = (await session.execute(select(Equipment.equipment_id, Equipment.aas_id))).all()
            resolved = resolve_equipment_ids(plans, [(row.equipment_id, row.aas_id) for row in rows])
            token = plan_token(resolved)
            self.last_scan_at = datetime.now(timezone.utc)
            self.packages = len(resolved)
            self.nodes = sum(len(plan.nodes) for plan in resolved)
            self.mappings = self.nodes
            if token == self._token:
                self.unchanged = True
                self.last_error = None
                return
            nodes, mappings = await apply_plans(session, resolved)
            await session.commit()
        self._token = token
        self.nodes = nodes
        self.mappings = mappings
        self.unchanged = False
        self.last_error = None
        self.runtime.nodes.invalidate()
        logger.info(
            "AASX 메타 동기화: packages=%d nodes=%d mappings=%d endpoint=%s",
            len(resolved), nodes, mappings, context.endpoint,
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "interval_s": self.interval,
            "last_scan_at": self.last_scan_at,
            "last_error": self.last_error,
            "packages": self.packages,
            "nodes": self.nodes,
            "mappings": self.mappings,
            "unchanged": self.unchanged,
        }
