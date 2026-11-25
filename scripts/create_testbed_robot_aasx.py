"""HH7 AAS 템플릿 v0.3 구조를 복제해 테스트베드 로봇 AASX를 만든다.

대상은 두산 M1509, 엡손 LS6-602, 이구스 DLE-DR-0001 이다.
정적 제원은 제조사 공개 사양이고, 수집 경로는 테스트베드에서 확인된 값만 적는다.
실시간 관절·TCP 값은 비워 둔다.

사용법 (camtic-aas-backend-viewer 루트):
    python -m scripts.create_testbed_robot_aasx
    python -m scripts.create_testbed_robot_aasx --upload http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse
import io
import json
import uuid
import urllib.request
from pathlib import Path

from basyx.aas import model
from basyx.aas.adapter import aasx
from basyx.aas.adapter.json import object_store_to_json, read_aas_json_file

from config import BASE_DIR

HH7_AASX = (
    BASE_DIR
    / "data/packages/Hyundai_HH7_AAS_Template_v0.3/package.aasx"
)
OUT_DIR = BASE_DIR / "data/packages"

SHELL_URN = "urn:physical-ai:aas:hyundai:hh7:template:v0.3"
ASSET_URN = "urn:physical-ai:asset-type:hyundai:hh7"
TEMPLATE_URN = "urn:physical-ai:template:hh7:0.3"
HH7_PREFIX = "urn:physical-ai:hh7:"


def _ml_name(ko: str, en: str) -> model.MultiLanguageNameType:
    return model.MultiLanguageNameType({"ko": ko, "en": en})


def _ml_text(ko: str, en: str) -> model.MultiLanguageTextType:
    return model.MultiLanguageTextType({"ko": ko, "en": en})


def _kids(element: model.Referable):
    if isinstance(element, model.Submodel):
        return element.submodel_element
    value = getattr(element, "value", None)
    if value is None:
        return ()
    try:
        return value
    except TypeError:
        return ()


def _find(element: model.Referable, name: str) -> model.Referable:
    for child in _kids(element):
        if getattr(child, "id_short", None) == name:
            return child
    raise KeyError(name)


def _add_string(parent, id_short: str, value: str, ko: str, en: str) -> None:
    parent.value.add(
        model.Property(
            id_short,
            model.datatypes.String,
            value=value,
            display_name=_ml_name(ko, en),
            description=_ml_text(ko, en),
        )
    )


def _set_desc(element: model.Referable, ko: str, en: str) -> None:
    element.description = _ml_text(ko, en)


def _keep_axes(collection, names: set[str]) -> None:
    for child in list(collection.value):
        if child.id_short not in names:
            collection.value.remove(child)


def _remove_named(parent, name: str) -> None:
    child = _find_optional(parent, name)
    if child is None:
        return
    if isinstance(parent, model.Submodel):
        parent.submodel_element.remove(child)
    else:
        parent.value.remove(child)


def _rename_orientation(kinematic, rename: dict) -> None:
    orientation = _find(kinematic, "TCPOrientation")
    old = _find(orientation, rename["from"])
    renamed = model.Property(
        rename["to"],
        old.value_type,
        value=old.value,
        display_name=_ml_name(rename["ko_name"], rename["en_name"]),
        description=_ml_text(rename["ko"], rename["en"]),
        semantic_id=old.semantic_id,
    )
    orientation.value.remove(old)
    for name in rename.get("drop", []):
        _remove_named(orientation, name)
    orientation.value.add(renamed)
    _set_desc(orientation, rename["collection_ko"], rename["collection_en"])


def _drop_submodel(store, shell, submodel: model.Submodel) -> None:
    for ref in list(shell.submodel):
        if any(key.value == submodel.id for key in ref.key):
            shell.submodel.remove(ref)
    store.discard(submodel)


def _load_hh7_json() -> str:
    with aasx.AASXReader(HH7_AASX) as reader:
        store = model.DictIdentifiableStore()
        files = aasx.DictSupplementaryFileContainer()
        reader.read_into(store, files)
    return object_store_to_json(store)


def _retarget(raw: str, spec: dict) -> model.DictIdentifiableStore:
    text = raw.replace(SHELL_URN, spec["shell_id"])
    text = text.replace(ASSET_URN, spec["asset_id"])
    text = text.replace(TEMPLATE_URN, spec["template_id"])
    text = text.replace(HH7_PREFIX, spec["urn_prefix"])
    text = text.replace('"idShort": "HH7"', f'"idShort": "{spec["id_short"]}"')
    text = text.replace("HH7", spec["id_short"])
    text = text.replace("Hyundai Robotics", spec["manufacturer"])
    return read_aas_json_file(io.StringIO(text), failsafe=False)


def _apply(store: model.DictIdentifiableStore, spec: dict) -> aasx.DictSupplementaryFileContainer:
    shell = next(obj for obj in store if isinstance(obj, model.AssetAdministrationShell))
    shell.display_name = _ml_name(spec["display_ko"], spec["display_en"])
    shell.description = _ml_text(spec["description_ko"], spec["description_en"])
    shell.administration.version = "0"
    shell.administration.revision = spec["revision"]
    shell.asset_information.asset_kind = model.AssetKind.INSTANCE

    by_short = {
        obj.id_short: obj
        for obj in store
        if isinstance(obj, model.Submodel)
    }
    identification = by_short["Identification"]
    for name, value in spec["identification"].items():
        _find(identification, name).value = value

    technical = by_short["TechnicalData"]
    axes = spec["axes"]
    _find(technical, "NumberOfAxes").value = model.datatypes.Int(len(axes))
    _find(technical, "Payload").value = float(spec["payload_kg"])
    _find(technical, "Reach").value = float(spec["reach_mm"])
    _find(technical, "Repeatability").value = float(spec["repeatability_mm"])
    _set_desc(technical, spec["technical_ko"], spec["technical_en"])
    _set_desc(_find(technical, "Payload"), spec["payload_ko"], spec["payload_en"])
    _set_desc(_find(technical, "Reach"), spec["reach_ko"], spec["reach_en"])
    _set_desc(
        _find(technical, "Repeatability"),
        spec["repeatability_ko"],
        spec["repeatability_en"],
    )
    catalog = model.SubmodelElementCollection(
        "CatalogSpecification",
        display_name=_ml_name("카탈로그 제원", "Catalog specification"),
        description=_ml_text(
            "제조사 공개 사양 중 TechnicalData 네 항목에 들어가지 않는 값.",
            "Published catalog values that do not fit the four TechnicalData properties.",
        ),
        value=[],
    )
    for name, value, ko, en in spec["catalog"]:
        _add_string(catalog, name, value, ko, en)
    technical.submodel_element.add(catalog)

    kinematic = by_short["RobotKinematicState"]
    axis_names = set(axes)
    _keep_axes(_find(kinematic, "JointPosition"), axis_names)
    if "JointSpeed" not in spec.get("drop_kinematic", []):
        _keep_axes(_find(kinematic, "JointSpeed"), axis_names)
    for note in spec.get("joint_notes", []):
        joint = _find(_find(kinematic, "JointPosition"), note["id"])
        _set_desc(joint, note["ko"], note["en"])
    if spec.get("orientation"):
        _rename_orientation(kinematic, spec["orientation"])
    for name in spec.get("drop_kinematic", []):
        _remove_named(kinematic, name)

    acquisition = by_short["DataAcquisitionConfiguration"]
    source = _find(acquisition, "SourceInterface")
    for name, value in spec["source"].items():
        _find(source, name).value = value
    _add_string(source, "Endpoint", spec["endpoint"], "접속 주소", "Endpoint")
    _add_string(source, "PlcRole", spec["plc_role"], "PLC 장비 서브모델", "PLC equipment submodel")
    joint_span = f"{axes[0]}..{axes[-1]}"
    mappings = _find(acquisition, "Mappings")
    for name in spec.get("drop_mappings", []):
        _remove_named(mappings, name)
    for name, fields in spec["mappings"].items():
        block = _find(mappings, name)
        for key, value in fields.items():
            if key == "Notes" and _find_optional(block, "Notes") is None:
                _add_string(block, "Notes", value, "비고", "Notes")
                continue
            prop = _find(block, key)
            if key == "SamplingIntervalMs" and value is None:
                prop.value = None
            elif key == "SamplingIntervalMs":
                prop.value = int(value)
            else:
                prop.value = value
        if name == "MapJointPosition":
            _find(block, "TargetAASPath").value = (
                f"RobotKinematicState/JointPosition/{joint_span}"
            )
        if name == "MapJointSpeed":
            _find(block, "TargetAASPath").value = (
                f"RobotKinematicState/JointSpeed/{joint_span}"
            )
    for block in mappings.value:
        fields = spec["mappings"].get(block.id_short, {})
        if "SamplingIntervalMs" in fields:
            continue
        interval = _find_optional(block, "SamplingIntervalMs")
        if interval is not None and interval.value == 10:
            interval.value = None

    readme = spec["readme"].encode("utf-8")
    mapping_csv = spec["mapping_csv"].encode("utf-8")
    report = spec["report"].encode("utf-8")
    files = aasx.DictSupplementaryFileContainer()
    readme_path = "/aasx/suppl/README.txt"
    csv_path = "/aasx/suppl/mapping.csv"
    report_path = "/aasx/suppl/prevalidation.md"
    files.add_file(readme_path, io.BytesIO(readme), "text/plain")
    files.add_file(csv_path, io.BytesIO(mapping_csv), "text/csv")
    files.add_file(report_path, io.BytesIO(report), "text/markdown")

    documentation = by_short["Documentation"]
    _find(documentation, "TemplateReadme").value = readme_path
    _find(documentation, "MappingTable").value = csv_path
    _find(documentation, "PreValidationReport").value = report_path
    _find(documentation, "DigitalTwinCadModel").value = None
    _find(documentation, "DigitalTwinUsdModel").value = None
    for name in spec.get("drop_submodels", []):
        _drop_submodel(store, shell, by_short[name])
    return files


def _find_optional(element, name: str):
    for child in _kids(element):
        if getattr(child, "id_short", None) == name:
            return child
    return None


def _write(package_id: str, store, files) -> Path:
    dest = OUT_DIR / package_id / "package.aasx"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with aasx.AASXWriter(dest) as writer:
        writer.write_all_aas_objects("/aasx/data.json", store, files)
    return dest


def _upload(url: str, path: Path) -> dict:
    boundary = uuid.uuid4().hex
    payload = path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.parent.name}.aasx"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        url.rstrip("/") + "/api/packages",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode())


def _docs(spec: dict) -> dict:
    rows = ["logical_field,raw_source,unit,verification"]
    for name, fields in spec["mappings"].items():
        rows.append(
            ",".join(
                [
                    name,
                    '"' + fields["RawSourceField"].replace('"', "'") + '"',
                    '"' + fields.get("Unit", "").replace('"', "'") + '"',
                    fields["VerificationStatus"],
                ]
            )
        )
    catalog_lines = "\n".join(f"- {ko}: {value}" for _, value, ko, _ in spec["catalog"])
    readme = f"""{spec['display_ko']}

구조는 Hyundai HH7 AAS Template v0.3 과 같은 서브모델이다.
Identification, TechnicalData, RobotKinematicState, RobotOperationalState,
DataAcquisitionConfiguration, HistoricalDataConfiguration, Documentation.

카탈로그 제원
- 제조사: {spec['manufacturer']}
- 모델: {spec['identification']['ProductDesignation']}
- 축 수: {len(spec['axes'])}
- 가반하중: {spec['payload_kg']} kg
- 도달/작업거리: {spec['reach_mm']} mm
- 반복정밀도: {spec['repeatability_mm']} mm
{catalog_lines}

테스트베드
- 역할: {spec['plc_role']}
- 접속: {spec['endpoint']}
- 프로토콜: {spec['source']['ApplicationProtocol']}

{spec.get("live_note", "실시간 JointPosition, JointSpeed, TCP 값은 비어 있다. 수집기가 채운다.")}
CAD/USD 는 이 패키지에 넣지 않았다.
"""
    report = f"""# {spec['id_short']} 사전검증

채운 값
- Identification 과 TechnicalData 의 축 수, 가반하중, 도달거리, 반복정밀도
- CatalogSpecification 의 제조사 공개 사양
- DataAcquisitionConfiguration 의 테스트베드에서 확인된 수집 경로

비운 값
- 관절 위치·속도, TCP, 운전상태의 실시간 값
- 디지털 트윈 CAD/USD

남은 확인
{spec['open_items']}
"""
    return {"readme": readme, "mapping_csv": "\n".join(rows) + "\n", "report": report}


def _specs() -> list[dict]:
    doosan_maps = {
        "MapJointPosition": {
            "RawSourceField": "Modbus holding 270-275, i16, scale 0.1. 두산 슬레이브 관절위치와 테스트베드 m1509_memory_map.json 이 같다.",
            "Unit": "deg",
            "VerificationStatus": "TESTBED_MAP",
            "SamplingPolicy": "FIXED_RATE",
            "SamplingIntervalMs": None,
            "Notes": "수집 주기는 메모리 맵에 없다. 템플릿의 10 ms 는 쓰지 않는다.",
        },
        "MapJointSpeed": {
            "RawSourceField": "Modbus holding 280-285, i16, scale 0.1. m1509_memory_map.json.",
            "Unit": "deg/s",
            "VerificationStatus": "TESTBED_MAP",
            "SamplingIntervalMs": None,
        },
        "MapTCPPosition": {
            "RawSourceField": "Modbus holding 400-402, i16, scale 0.01. m1509_memory_map.json tcp_x/y/z.",
            "Unit": "mm",
            "VerificationStatus": "TESTBED_MAP",
            "SamplingIntervalMs": None,
        },
        "MapTCPOrientation": {
            "RawSourceField": "Modbus holding 403-405, i16, scale 0.01. m1509_memory_map.json tcp_rx/ry/rz.",
            "Unit": "deg",
            "VerificationStatus": "TESTBED_MAP",
            "SamplingIntervalMs": None,
            "Notes": "오일러 순서와 부호는 컨트롤러 설정 확인이 필요하다.",
        },
        "MapTCPSpeed": {
            "RawSourceField": "Modbus holding 410-412, i16, scale 0.01. tcp_vx/vy/vz. AAS 속성은 스칼라 하나라 벡터 대응은 아직이다.",
            "Unit": "mm/s",
            "VerificationStatus": "PARTIAL",
            "SamplingIntervalMs": None,
        },
    }
    scara_maps = {
        "MapJointPosition": {
            "RawSourceField": "RC90B Main.prg JSON Joint.J1..J4.Position. Agl(1..4). J1 J2 J4 deg, J3 mm. 약 1초 주기.",
            "Unit": "J1 J2 J4 deg, J3 mm",
            "VerificationStatus": "TESTBED_PROGRAM",
            "SamplingPolicy": "FIXED_RATE",
            "SamplingIntervalMs": 1000,
        },
        "MapTCPPosition": {
            "RawSourceField": "Main.prg Cartesian.Position. CurPos 의 CX CY CZ.",
            "Unit": "mm",
            "VerificationStatus": "TESTBED_PROGRAM",
            "SamplingIntervalMs": 1000,
        },
        "MapTCPOrientation": {
            "RawSourceField": "Main.prg Cartesian.Orientation.Rz = CU(CurPos).",
            "Unit": "deg",
            "VerificationStatus": "TESTBED_PROGRAM",
            "SamplingIntervalMs": 1000,
            "TargetAASPath": "RobotKinematicState/TCPOrientation/Rz",
            "OPCUANodeId": "ns=2;s=Epson_LS6_602/RobotKinematicState/TCPOrientation/Rz",
            "Notes": "스카라는 Rz 하나만 보낸다.",
        },
    }
    delta_maps = {
        "MapJointPosition": {
            "RawSourceField": "LabVIEW Robot-Delta J13 J14 J15 Position. DeltaRAWdata 열 1, I32, scale 1. 테스트베드 AAS 축 이름은 J1 J2 J3 이고 클러스터 이름은 J13 J14 J15 다.",
            "Unit": "TBD",
            "VerificationStatus": "PARTIAL",
            "SamplingIntervalMs": None,
            "Notes": "PLC holding 1024부터 10워드만 주소가 확정돼 있다. 관절 3축(6워드)과 1:1로 단정하지 않는다.",
        },
        "MapJointSpeed": {
            "RawSourceField": "DeltaRAWdata 열 2, I32, scale 0.1. J13 J14 J15 Speed.",
            "Unit": "TBD",
            "VerificationStatus": "PARTIAL",
            "SamplingIntervalMs": None,
        },
        "MapTCPPosition": {
            "RawSourceField": "DeltaRAWdata 열 4, I32 에 1000을 곱해 X Y Z. 단위는 미확인.",
            "Unit": "TBD",
            "VerificationStatus": "PARTIAL",
            "SamplingIntervalMs": None,
        },
    }
    robots = [
        {
            "package_id": "Doosan_M1509_AAS_Template_v0.3",
            "shell_id": "urn:physical-ai:aas:doosan:m1509:T1Z5B7:v0.3",
            "asset_id": "urn:physical-ai:asset:doosan:m1509:T1Z5B7",
            "template_id": "urn:physical-ai:template:doosan:m1509:0.3",
            "urn_prefix": "urn:physical-ai:doosan:m1509:",
            "id_short": "Doosan_M1509",
            "revision": "3",
            "manufacturer": "Doosan Robotics",
            "display_ko": "두산 M1509 협동로봇 AAS",
            "display_en": "Doosan M1509 collaborative robot AAS",
            "description_ko": "HH7 v0.3 템플릿 구조의 테스트베드 두산 M1509 인스턴스. 시리얼 T1Z5B7.",
            "description_en": "Testbed Doosan M1509 instance on the HH7 v0.3 template structure. Serial T1Z5B7.",
            "axes": ["J1", "J2", "J3", "J4", "J5", "J6"],
            "payload_kg": 15.0,
            "reach_mm": 900.0,
            "repeatability_mm": 0.03,
            "payload_ko": "가반하중 15 kg",
            "payload_en": "Payload 15 kg",
            "reach_ko": "최대 작업반경 900 mm",
            "reach_en": "Maximum reach 900 mm",
            "repeatability_ko": "반복정밀도 ±0.03 mm. 속성값은 절대값.",
            "repeatability_en": "Repeatability ±0.03 mm. The property stores the absolute value.",
            "technical_ko": "두산 M1509 공개 사양. 실시간 값과 분리한다.",
            "technical_en": "Published Doosan M1509 specifications, separate from live values.",
            "identification": {
                "ManufacturerName": "Doosan Robotics",
                "ProductDesignation": "M1509",
                "AssetType": "CollaborativeRobot",
                "SerialNumber": "T1Z5B7",
                "InstallationLocation": "CAMTIC Physical AI Testbed",
                "ControllerModel": "CS-01",
            },
            "catalog": [
                ("MassKg", "32", "본체 질량 kg", "Arm mass kg"),
                ("MaxTcpSpeed", "1 m/s", "최대 TCP 속도", "Maximum TCP speed"),
                ("IpRating", "IP54", "보호 등급", "IP rating"),
                ("Mounting", "Any orientation", "설치 방향", "Mounting"),
                ("OperatingTemperature", "0–45 °C", "운전 온도", "Operating temperature"),
                ("Noise", "< 65 dB", "소음", "Noise"),
                (
                    "JointLimits",
                    "J1 ±360°/150°/s; J2 ±360°/150°/s; J3 ±150°/180°/s; J4 ±360°/225°/s; J5 ±360°/225°/s; J6 ±360°/225°/s",
                    "관절 범위와 최대 속도",
                    "Joint range and maximum speed",
                ),
                ("TorqueSensing", "6축 각각 토크 센서", "토크 센서", "Torque sensing"),
            ],
            "source": {
                "PhysicalInterface": "Ethernet",
                "ApplicationProtocol": "Modbus TCP slave",
                "EdgeRole": "Holding register 수집 후 AAS 매핑",
            },
            "endpoint": "192.168.1.100:502",
            "plc_role": "Robot01_Doosan",
            "mappings": doosan_maps,
            "joint_notes": [],
            "open_items": "- 속도·전류·온도·토크·TCP 레지스터는 m1509_memory_map.json 기준이다. 270번 관절위치만 두산 슬레이브 표와 맞춰 두었다.\n- TCP 속도는 축별 레지스터이고 AAS 속성은 스칼라 하나다.\n- 수집 주기는 미확인.",
        },
        {
            "package_id": "Epson_LS6_602_AAS_Template_v0.1",
            "shell_id": "urn:physical-ai:aas:epson:ls6-602:testbed:v0.1",
            "asset_id": "urn:physical-ai:asset:epson:ls6-602:testbed",
            "template_id": "urn:physical-ai:template:epson:ls6-602:0.1",
            "urn_prefix": "urn:physical-ai:epson:ls6-602:",
            "id_short": "Epson_LS6_602",
            "revision": "2",
            "manufacturer": "Epson",
            "display_ko": "엡손 LS6-602 스카라 AAS",
            "display_en": "Epson LS6-602 SCARA AAS",
            "description_ko": "HH7 v0.3 템플릿 구조의 테스트베드 엡손 LS6-602 인스턴스. 컨트롤러 RC90B R9W3042606.",
            "description_en": "Testbed Epson LS6-602 instance on the HH7 v0.3 template structure. Controller RC90B R9W3042606.",
            "axes": ["J1", "J2", "J3", "J4"],
            "payload_kg": 6.0,
            "reach_mm": 600.0,
            "repeatability_mm": 0.02,
            "payload_ko": "최대 가반하중 6 kg. 정격은 2 kg.",
            "payload_en": "Maximum payload 6 kg. Rated payload is 2 kg.",
            "reach_ko": "암 길이 600 mm. 1암 325 mm, 2암 275 mm.",
            "reach_en": "Arm length 600 mm. Arm 1 is 325 mm and arm 2 is 275 mm.",
            "repeatability_ko": "J1+J2 반복정밀도 ±0.02 mm. J3 ±0.01 mm, J4 ±0.01° 는 CatalogSpecification.",
            "repeatability_en": "J1+J2 repeatability ±0.02 mm. J3 and J4 are in CatalogSpecification.",
            "technical_ko": "엡손 LS6-602 표준형 공개 사양. 프로젝트 표기는 접미사 없는 LS6-602 라 표준형 Z 200 mm를 넣었다.",
            "technical_en": "Published standard Epson LS6-602 data. The project name has no S/C suffix, so the 200 mm Z stroke of the standard model is used.",
            "identification": {
                "ManufacturerName": "Epson",
                "ProductDesignation": "LS6-602",
                "AssetType": "SCARA",
                "SerialNumber": "",
                "InstallationLocation": "CAMTIC Physical AI Testbed",
                "ControllerModel": "RC90B",
            },
            "catalog": [
                ("RatedPayloadKg", "2", "정격 가반하중 kg", "Rated payload kg"),
                ("ZStrokeMm", "200", "Z 스트로크 mm. 표준형. 클린룸형은 170", "Z stroke mm for the standard model. Cleanroom is 170"),
                ("RepeatabilityJ3Mm", "0.01", "J3 반복정밀도 mm", "J3 repeatability mm"),
                ("RepeatabilityJ4Deg", "0.01", "J4 반복정밀도 deg", "J4 repeatability deg"),
                ("MassKg", "17", "본체 질량 kg. 케이블 제외", "Manipulator mass kg, cables excluded"),
                ("Joint4Inertia", "rated 0.01 / max 0.12 kg·m²", "J4 허용 관성", "J4 allowable inertia"),
                ("DownForceN", "100", "J3 하향 힘 N", "J3 downward force N"),
                ("JointLimits", "J1 ±132°; J2 ±150°; J3 200 mm; J4 ±360°", "동작 범위", "Travel limits"),
                ("ControllerIdentity", "RC90B R9W3042606, 192.168.1.121, RC+ 7.5.4", "컨트롤러 식별", "Controller identity"),
                (
                    "VariantNote",
                    "프로젝트 문자열은 LS6-602. 표준형 LS6-602S / LS6-B602S 의 Z 200 mm를 사용. 클린룸 602C 의 Z 170 mm와는 구분하지 못했다.",
                    "형번 주석",
                    "Variant note",
                ),
            ],
            "source": {
                "PhysicalInterface": "Ethernet",
                "ApplicationProtocol": "TCP CRLF JSON. 컨트롤러가 192.168.1.2:5002 로 송신. 명령은 컨트롤러 5003.",
                "EdgeRole": "SPEL+ Main.prg 가 컨트롤러 안에서 JSON을 보낸다. RC+ SDK 경유가 아니다.",
            },
            "endpoint": "192.168.1.121 TCP 5002/5003",
            "plc_role": "Robot03_Scara",
            "mappings": scara_maps,
            "live_note": "실시간으로 남긴 값은 관절 위치 J1–J4, TCP X Y Z, 자세 Rz 이다. 관절 속도, Rx, Ry, TCP 속도, 가속도, 툴 오프셋, 운전상태는 이 스카라 수집에 없어 뺐다.",
            "orientation": {
                "from": "Rx",
                "to": "Rz",
                "ko_name": "Rz",
                "en_name": "Rz",
                "ko": "스카라 TCP 자세. CU(CurPos)의 Rz. 단위 deg.",
                "en": "SCARA TCP orientation. Rz from CU(CurPos), in degrees.",
                "collection_ko": "TCP 자세. 이 스카라는 Rz 만 있다.",
                "collection_en": "TCP orientation. This SCARA has Rz only.",
                "drop": ["Ry", "Rz"],
            },
            "drop_kinematic": ["JointSpeed", "ToolPoseTCPOffset", "TCPSpeed", "AxisAcceleration"],
            "drop_mappings": [
                "MapJointSpeed",
                "MapTCPSpeed",
                "MapAxisAcceleration",
                "MapToolPose",
                "MapProgramTaskState",
                "MapRobotMode",
                "MapSystemStatus",
            ],
            "drop_submodels": ["RobotOperationalState"],
            "joint_notes": [
                {
                    "id": "J3",
                    "ko": "직동축. Main.prg 주석상 단위는 mm.",
                    "en": "Prismatic axis. Main.prg comments the unit as mm.",
                }
            ],
            "open_items": "- 매니퓰레이터 시리얼은 프로젝트에 없다. R9W3042606 은 컨트롤러 이름이다.\n- S/C 접미사가 없어 Z 스트로크는 표준형 200 mm로 적었다.\n- RealTorque(1..4)는 컬렉터에 있으나 속도가 아니라 -1..1 비율이고, 아직 이 템플릿에 넣지 않았다.",
        },
        {
            "package_id": "Igus_DLE_DR_0001_AAS_Template_v0.1",
            "shell_id": "urn:physical-ai:aas:igus:dle-dr-0001:testbed:v0.1",
            "asset_id": "urn:physical-ai:asset:igus:dle-dr-0001:testbed",
            "template_id": "urn:physical-ai:template:igus:dle-dr-0001:0.1",
            "urn_prefix": "urn:physical-ai:igus:dle-dr-0001:",
            "id_short": "Igus_DLE_DR_0001",
            "revision": "1",
            "manufacturer": "igus",
            "display_ko": "이구스 DLE-DR-0001 델타로봇 AAS",
            "display_en": "igus DLE-DR-0001 delta robot AAS",
            "description_ko": "HH7 v0.3 템플릿 구조의 테스트베드 이구스 DLE-DR-0001 인스턴스. LabVIEW 모델 상수 igus_DLE-DR-0001.",
            "description_en": "Testbed igus DLE-DR-0001 instance on the HH7 v0.3 template structure. LabVIEW model constant igus_DLE-DR-0001.",
            "axes": ["J1", "J2", "J3"],
            "payload_kg": 5.0,
            "reach_mm": 360.0,
            "repeatability_mm": 0.5,
            "payload_ko": "최대 가반하중 5 kg",
            "payload_en": "Maximum payload 5 kg",
            "reach_ko": "높이 75 mm에서 작업공간 지름 360 mm. 반경이 아니다.",
            "reach_en": "Working-space diameter 360 mm at 75 mm height, not a radius.",
            "repeatability_ko": "중심 TCP 위치정밀도 ±0.5 mm. 이구스 자료의 accuracy.",
            "repeatability_en": "Centered TCP accuracy ±0.5 mm from the igus data sheet.",
            "technical_ko": "igus drylin DLE-DR-0001 공개 사양. 3축 델타.",
            "technical_en": "Published igus drylin DLE-DR-0001 data. 3-axis delta.",
            "identification": {
                "ManufacturerName": "igus",
                "ProductDesignation": "DLE-DR-0001",
                "AssetType": "DeltaRobot",
                "SerialNumber": "",
                "InstallationLocation": "CAMTIC Physical AI Testbed",
                "ControllerModel": "",
            },
            "catalog": [
                ("WorkspaceDiameterMm", "360", "높이 75 mm 작업공간 지름", "Working-space diameter at 75 mm height"),
                ("WorkspaceReferenceHeightMm", "75", "지름을 정의한 높이", "Height at which the diameter is defined"),
                ("MaxProcessForceN", "100", "반경 0에서 최대 공정력 N", "Maximum process force at radius 0"),
                ("MaxTrackSpeed", "3 m/s", "최대 궤적 속도", "Maximum track speed"),
                ("MaxAcceleration", "60 m/s²", "최대 가속도", "Maximum acceleration"),
                ("MassKg", "15", "본체 질량 kg", "Mass kg"),
                ("Dynamics", "500 g에서 최소 60 picks/min", "픽 성능", "Pick rate"),
                ("Motor", "NEMA 23 XL, 24/48 V, 4.2 A", "모터", "Motor"),
                (
                    "VariantNote",
                    "DLE-DR-0001 은 조립 키트다. 제어기 포함 형번인지, Dryve D1 또는 iRC가 달렸는지는 테스트베드에서 확인되지 않았다.",
                    "형번 주석",
                    "Variant note",
                ),
            ],
            "source": {
                "PhysicalInterface": "Ethernet",
                "ApplicationProtocol": "PLC Modbus TCP. 로봇 REST가 아니다.",
                "EdgeRole": "PLC holding 을 읽어 Robot-Delta 클러스터로 변환",
            },
            "endpoint": "192.168.1.120 unit 2 port 502, holding 1024-1033 read, 1030-1031 write",
            "plc_role": "Robot02_Delta",
            "mappings": delta_maps,
            "drop_kinematic": ["TCPOrientation"],
            "drop_mappings": ["MapTCPOrientation"],
            "joint_notes": [
                {"id": "J1", "ko": "테스트베드 클러스터 이름 J13.", "en": "Named J13 in the testbed cluster."},
                {"id": "J2", "ko": "테스트베드 클러스터 이름 J14.", "en": "Named J14 in the testbed cluster."},
                {"id": "J3", "ko": "테스트베드 클러스터 이름 J15.", "en": "Named J15 in the testbed cluster."},
            ],
            "open_items": "- 모델 문자열과 3축 구성은 LabVIEW 상수로 확인된다. 제어기 포함 여부는 미확인.\n- 관절·좌표의 Modbus 주소는 1024부터 10워드와 개수가 맞지 않아 필드 단위로 묶지 않았다.\n- 좌표에 곱하는 1000의 단위는 미확인.",
        },
    ]
    for spec in robots:
        spec.update(_docs(spec))
    return robots


def build_all() -> list[Path]:
    if not HH7_AASX.is_file():
        raise SystemExit(f"HH7 template not found: {HH7_AASX}")
    raw = _load_hh7_json()
    written: list[Path] = []
    for spec in _specs():
        store = _retarget(raw, spec)
        files = _apply(store, spec)
        path = _write(spec["package_id"], store, files)
        written.append(path)
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", default="", help="AAS 서버 루트. 예: http://127.0.0.1:8100")
    args = parser.parse_args()
    written = build_all()
    if not args.upload:
        return
    for path in written:
        result = _upload(args.upload, path)
        package = result.get("package", {})
        print(
            "uploaded",
            package.get("id"),
            "shells",
            package.get("counts", {}).get("shells"),
            "submodels",
            package.get("counts", {}).get("submodels"),
            "artifacts",
            package.get("artifactCount"),
        )


if __name__ == "__main__":
    main()
