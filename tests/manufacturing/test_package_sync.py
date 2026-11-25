"""AASX 템플릿 → 설비 메타 계획. DB 에는 접속하지 않는다."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.manufacturing.ingest import NodeCache
from app.manufacturing.package_sync import (
    EquipmentPlan,
    NodePlan,
    PackageSync,
    SyncContext,
    apply_plans,
    discover_plans,
    equipment_id_for,
    plan_from_records,
    resolve_equipment_ids,
)
from app.opcua_export import load_engineering_nodes

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "data" / "packages"
ENDPOINT = "opc.tcp://field.test:4840"
CATALOG_AAS = frozenset({
    "urn:physical-ai:plc:aas:template:v0.4",
    "urn:physical-ai:aas:hyundai:hh7:template:v0.3",
    "urn:physical-ai:aas:epson:ls6-602:testbed:v0.1",
    "urn:physical-ai:aas:igus:dle-dr-0001:testbed:v0.1",
    "urn:physical-ai:aas:camtic:st-mini:template:v0.1",
})


def _by_path(plan: EquipmentPlan) -> dict[str, NodePlan]:
    return {node.path: node for node in plan.nodes}


def _plan(package_id: str, *, namespace=None, allowed=frozenset()) -> EquipmentPlan:
    records = load_engineering_nodes(PACKAGES / package_id / "opcua", variables_only=False)
    plan = plan_from_records(
        package_id, records, namespace=namespace, endpoint=ENDPOINT, allowed_aas_ids=allowed,
    )
    assert plan is not None
    return plan


def test_equipment_id_keeps_safe_names_and_folds_plc_folder():
    assert equipment_id_for("Hyundai_HH7_AAS_Template_v0.3") == "Hyundai_HH7_AAS_Template_v0.3"
    assert equipment_id_for("Epson_LS6_602_AAS_Template_v0.1") == "Epson_LS6_602_AAS_Template_v0.1"
    assert equipment_id_for("PLC_AAS_Template_v0.4 (1)") == "PLC_AAS_Template_v0.4_1"
    assert equipment_id_for("   ") == ""


def test_catalog_packages_become_equipment_plans_and_skip_other_folders():
    plans = discover_plans(
        PACKAGES, "opcua", namespace=1, endpoint=ENDPOINT, allowed_aas_ids=CATALOG_AAS,
    )
    by_id = {plan.equipment_id: plan for plan in plans}
    assert set(by_id) == {
        "Hyundai_HH7_AAS_Template_v0.3",
        "Epson_LS6_602_AAS_Template_v0.1",
        "Igus_DLE_DR_0001_AAS_Template_v0.1",
        "CAMTIC_ST_mini_AAS_Template_v0.1",
        "PLC_AAS_Template_v0.4_1",
    }
    assert by_id["Hyundai_HH7_AAS_Template_v0.3"].equipment_type == "ROBOT"
    assert by_id["CAMTIC_ST_mini_AAS_Template_v0.1"].equipment_type == "AMR"
    assert by_id["PLC_AAS_Template_v0.4_1"].equipment_type == "PLC"
    assert all(plan.opcua_endpoint == ENDPOINT and plan.aas_id in CATALOG_AAS for plan in plans)


def test_hh7_registers_tsdb_signals_and_event_status_without_static_identity():
    plan = _plan("Hyundai_HH7_AAS_Template_v0.3", allowed=CATALOG_AAS)
    nodes = _by_path(plan)
    joint = nodes["JointPosition.J1"]
    assert joint.data_type == "Double"
    assert joint.unit == "deg"
    assert joint.sampling_ms == 10
    assert joint.mapping_type == "TIME_SERIES"
    assert joint.submodel_id == "urn:physical-ai:hh7:submodel:RobotKinematicState"
    assert joint.node_id == "ns=1;s=Hyundai_HH7_AAS_Template_v0.3/HH7/RobotKinematicState/JointPosition/J1"
    assert {f"JointPosition.J{i}" for i in range(1, 7)} <= set(nodes)
    assert nodes["ProgramTaskState"].mapping_type == "STATUS"
    assert nodes["ToolPoseTCPOffset.Position.X"].mapping_type == "TIME_SERIES"
    assert "ManufacturerName" not in nodes
    assert plan.manufacturer == "Hyundai Robotics"
    assert plan.model_name == "HH7"

    renamed = _plan("Hyundai_HH7_AAS_Template_v0.3", namespace=2, allowed=CATALOG_AAS)
    assert _by_path(renamed)["JointPosition.J1"].node_id.startswith("ns=2;")


def test_plc_io_targets_are_time_series_and_mapping_rows_are_not():
    plan = _plan("PLC_AAS_Template_v0.4 (1)", allowed=CATALOG_AAS)
    nodes = _by_path(plan)
    grip = next(node for node in plan.nodes if node.path == "Handshake.GripWaitRequest" and "Robot01_Doosan" in node.node_id)
    assert grip.mapping_type == "TIME_SERIES"
    assert grip.data_type == "Boolean"
    assert grip.node_id.endswith("/Robot01_Doosan/Handshake/GripWaitRequest")
    assert "Map_D2000.PLCAddress" not in nodes
    assert "AssetType" not in nodes
    assert plan.manufacturer is None
    assert plan.equipment_name == "피지컬 AI PLC AAS 템플릿 v0.4"


def test_amr_splits_tsdb_and_event_targets():
    plan = _plan("CAMTIC_ST_mini_AAS_Template_v0.1", allowed=CATALOG_AAS)
    nodes = _by_path(plan)
    assert nodes["CurrentBatteryCapacity"].mapping_type == "TIME_SERIES"
    assert nodes["Coordinates.X"].mapping_type == "TIME_SERIES"
    assert nodes["OperationalState"].mapping_type == "STATUS"
    assert plan.manufacturer == "AgileX Robotics"
    assert plan.model_name == "SCOUT MINI"


def test_existing_aas_id_keeps_the_field_equipment_id():
    plan = _plan("Hyundai_HH7_AAS_Template_v0.3", allowed=CATALOG_AAS)
    resolved = resolve_equipment_ids([plan], [("ROBOT_FIELD_01", plan.aas_id)])
    assert resolved[0].equipment_id == "ROBOT_FIELD_01"
    assert resolved[0].nodes == plan.nodes


def test_apply_upserts_metadata_and_leaves_enabled_alone_on_conflict():
    plan = EquipmentPlan(
        package_id="Pkg",
        equipment_id="PLC_CELL",
        equipment_type="PLC",
        equipment_name="Cell PLC",
        manufacturer="Camtic",
        model_name="Cell",
        serial_number=None,
        aas_id="urn:aas:plc",
        opcua_endpoint=ENDPOINT,
        nodes=(NodePlan(
            node_id="ns=1;s=Pkg/Shell/Robot01/Grip",
            node_name="Grip",
            data_type="Boolean",
            unit=None,
            sampling_ms=100,
            submodel_id="urn:sm:robot",
            path="Grip",
            mapping_type="TIME_SERIES",
        ),),
    )

    class Session:
        def __init__(self):
            self.sql: list[str] = []

        async def execute(self, stmt, params=None):
            compiled = stmt.compile(dialect=postgresql.dialect())
            self.sql.append(" ".join(str(compiled).split()))
            return SimpleNamespace(all=lambda: [])

    session = Session()
    asyncio.run(apply_plans(session, [plan]))
    equipment, node, mapping = session.sql
    assert equipment.startswith("INSERT INTO manufacturing.equipment")
    assert "ON CONFLICT (equipment_id) DO UPDATE" in equipment
    equipment_set = equipment.split("DO UPDATE SET", 1)[1]
    assert "enabled" not in equipment_set and "data_source" not in equipment_set
    assert "INSERT INTO manufacturing.equipment_node" in node
    assert "node_pk" not in node.split("VALUES", 1)[0]
    assert "ON CONFLICT (equipment_id, node_id) DO UPDATE" in node
    assert "enabled" not in node.split("DO UPDATE SET", 1)[1]
    assert "INSERT INTO manufacturing.aas_node_mapping" in mapping
    assert "mapping_id" not in mapping.split("VALUES", 1)[0]
    assert "ON CONFLICT (equipment_id, aas_submodel_id, aas_id_short_path, mapping_type) DO UPDATE" in mapping


def test_sync_skips_a_second_write_when_the_package_is_unchanged(tmp_path: Path):
    package = tmp_path / "Pkg"
    opcua = package / "opcua"
    opcua.mkdir(parents=True)
    (opcua / "engineering.csv").write_text(
        "NodeId,BrowseName,DisplayName,NodeClass,DataType,ParentNodeId,AasPath,AasId,ModelType,SemanticId,ValueType,Value,AccessLevel\n"
        "ns=1;s=Pkg,Pkg,Pkg,Object,,i=85,/Pkg,Pkg,AasxPackage,,,,\n"
        "ns=1;s=Pkg/Shell,Shell,Demo PLC,Object,,ns=1;s=Pkg,/Pkg/Shell,urn:aas:plc,AssetAdministrationShell,,,\n"
        "ns=1;s=Pkg/Shell/Robot01,Robot01,R,Object,,ns=1;s=Pkg/Shell,/Pkg/Shell/Robot01,urn:sm:robot,Submodel,,,,\n"
        "ns=1;s=Pkg/Shell/Robot01/Grip,Grip,grip,Variable,Boolean,ns=1;s=Pkg/Shell/Robot01,/Pkg/Shell/Robot01/Grip,,Property,,xs:boolean,false,\n"
        "ns=1;s=Pkg/Shell/IOInterfaceMapping,IOInterfaceMapping,io,Object,,ns=1;s=Pkg/Shell,/Pkg/Shell/IOInterfaceMapping,urn:sm:io,Submodel,,,,\n"
        "ns=1;s=Pkg/Shell/IOInterfaceMapping/Map/AASPath,AASPath,path,Variable,String,ns=1;s=Pkg/Shell/IOInterfaceMapping,/Pkg/Shell/IOInterfaceMapping/Map/AASPath,,Property,,xs:string,Robot01/Grip,\n"
        "ns=1;s=Pkg/Shell/Identification/AssetType,AssetType,t,Variable,String,ns=1;s=Pkg/Shell/Identification,/Pkg/Shell/Identification/AssetType,,Property,,xs:string,PLC,\n",
        encoding="utf-8",
    )
    inserts: list[str] = []

    class Session:
        async def execute(self, stmt, params=None):
            sql = " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())
            if sql.upper().startswith("INSERT"):
                inserts.append(sql)
            return SimpleNamespace(all=lambda: [])

        async def commit(self):
            inserts.append("COMMIT")

    @asynccontextmanager
    async def sessions():
        yield Session()

    runtime = SimpleNamespace(
        cfg=SimpleNamespace(sync_interval_s=60),
        sessions=sessions,
        nodes=NodeCache(),
    )
    sync = PackageSync(
        runtime, tmp_path, "opcua",
        lambda: SyncContext(endpoint=ENDPOINT, namespace=1, aas_ids=frozenset({"urn:aas:plc"})),
    )
    asyncio.run(sync.sync_once())
    first = len(inserts)
    asyncio.run(sync.sync_once())
    assert first > 0 and inserts.count("COMMIT") == 1
    assert sync.unchanged is True
    assert sync.packages == 1 and sync.nodes == 1 and sync.last_error is None
