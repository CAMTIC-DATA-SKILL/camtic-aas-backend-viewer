"""KOSMO AGV-AMR AAS Template v1.0.0 구조로 CAMTIC ST-mini AASX를 만든다.

원본은 Nestfield AGV-AMR 레퍼런스 모델이다.
https://github.com/kosmo-nestfield/AAS-Reference-Models/releases/tag/v1.0.0_AGV-AMR_AAS

기종은 AgileX SCOUT MINI 베이스의 CAMTIC ST-mini 다.
구성과 토픽은 SCOUT-mini_AMR_control 매뉴얼에 적힌 것만 넣는다.
실시간 값은 비워 둔다.

사용법 (camtic-aas-backend-viewer 루트):
    python -m scripts.create_amr_aasx
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from basyx.aas import model
from basyx.aas.adapter import aasx

from config import BASE_DIR

REFERENCE_AASX = BASE_DIR / "scripts/reference/AGV-AMR_Template_AAS_v1.0.0.aasx"
OUT_DIR = BASE_DIR / "data/packages/CAMTIC_ST_mini_AAS_Template_v0.1"
XML_NAME = (
    "aasx/http___nestfield_co_kr_ids_aas_CloudDataSolution/"
    "http___nestfield_co_kr_ids_aas_CloudDataSolution.aas.xml"
)

NS = {
    "aas": "http://www.admin-shell.io/aas/2/0",
    "IEC": "http://www.admin-shell.io/IEC61360/2/0",
}
IEC61360 = "https://admin-shell.io/DataSpecificationTemplates/DataSpecificationIEC61360/3/0"

SHELL_ID = "urn:physical-ai:aas:camtic:st-mini:template:v0.1"
ASSET_ID = "urn:physical-ai:asset-type:camtic:st-mini"
TEMPLATE_ID = "urn:physical-ai:template:camtic:st-mini:0.1"

SUBMODEL_ORDER = (
    "Nameplate",
    "Identification",
    "TechnicalData",
    "OperationalData",
    "ManufacturerDocumentation",
    "Safety",
    "Alarm",
    "CAD",
    "DataAcquisitionConfiguration",
    "HistoricalDataConfiguration",
    "Documentation",
)
SUBMODEL_LABELS = {
    "Nameplate": ("명판", "Nameplate"),
    "Identification": ("식별", "Identification"),
    "TechnicalData": ("기술 데이터", "Technical data"),
    "OperationalData": ("운전 데이터", "Operational data"),
    "ManufacturerDocumentation": ("제조사 문서", "Manufacturer documentation"),
    "Safety": ("안전", "Safety"),
    "Alarm": ("알람", "Alarm"),
    "CAD": ("CAD", "CAD"),
    "DataAcquisitionConfiguration": ("수집 설정", "Data acquisition configuration"),
    "HistoricalDataConfiguration": ("이력 설정", "Historical data configuration"),
    "Documentation": ("문서", "Documentation"),
}

VALUE_TYPES = {
    "string": model.datatypes.String,
    "integer": model.datatypes.Integer,
    "int": model.datatypes.Int,
    "double": model.datatypes.Double,
    "boolean": model.datatypes.Boolean,
    "date": model.datatypes.Date,
    "dateTime": model.datatypes.DateTime,
    "DurationString": model.datatypes.String,
    "Operational": model.datatypes.String,
}

_UNSET = object()


def _ml_name(ko: str, en: str) -> model.MultiLanguageNameType:
    return model.MultiLanguageNameType({"ko": ko, "en": en})


def _ml_text(ko: str, en: str) -> model.MultiLanguageTextType:
    return model.MultiLanguageTextType({"ko": ko, "en": en})


def _semantic(_key_type: str, value: str) -> model.ExternalReference:
    # AAS v2는 외부 IRI도 Submodel 키로 적었다. v3 외부 참조의 첫 키는 GlobalReference 여야 한다.
    return model.ExternalReference((model.Key(model.KeyTypes.GLOBAL_REFERENCE, value),))


def _text(element: ET.Element, name: str) -> str:
    node = element.find(f"aas:{name}", NS)
    return (node.text or "").strip() if node is not None and node.text else ""


def _semantic_of(element: ET.Element) -> tuple[str, str] | None:
    key = element.find("aas:semanticId/aas:keys/aas:key", NS)
    if key is None or not (key.text or "").strip():
        return None
    return key.get("type") or "GlobalReference", key.text.strip()


def _children(parent: ET.Element, wrapper: str) -> list[ET.Element]:
    found = parent.find(wrapper, NS)
    if found is None:
        return []
    elements: list[ET.Element] = []
    for wrap in found.findall("aas:submodelElement", NS):
        kids = list(wrap)
        if kids:
            elements.append(kids[0])
    return elements


def _sanitize(id_short: str) -> str:
    return id_short.replace(".", "")


def _rename_note(original: str) -> model.MultiLanguageTextType | None:
    if "." not in original:
        return None
    return _ml_text(
        f"원본 템플릿 idShort는 {original} 이다. 뷰어가 경로를 점으로 나누므로 점을 뺐다.",
        f"The reference template idShort is {original}. The dot was removed because paths are split on dots.",
    )


def _build_element(element: ET.Element) -> model.SubmodelElement:
    kind = element.tag.split("}")[-1]
    original = _text(element, "idShort")
    id_short = _sanitize(original)
    category = _text(element, "category") or None
    semantic = _semantic_of(element)
    semantic_id = _semantic(*semantic) if semantic else None
    description = _rename_note(original)
    if kind == "property" and _text(element, "valueType") == "langString":
        return model.MultiLanguageProperty(
            id_short,
            category=category,
            description=description,
            semantic_id=semantic_id,
        )
    if kind == "property":
        value_type_name = _text(element, "valueType")
        value_type = VALUE_TYPES.get(value_type_name, model.datatypes.String)
        return model.Property(
            id_short,
            value_type,
            category=category,
            description=description,
            semantic_id=semantic_id,
        )
    if kind == "file":
        return model.File(
            id_short,
            content_type=_text(element, "mimeType") or None,
            category=category,
            description=description,
            semantic_id=semantic_id,
        )
    if kind == "submodelElementCollection":
        collection = model.SubmodelElementCollection(
            id_short,
            category=category,
            description=description,
            semantic_id=semantic_id,
            value=[],
        )
        for child in _children(element, "aas:value"):
            collection.value.add(_build_element(child))
        return collection
    raise ValueError(f"unsupported element {kind} {original}")


def _load_reference() -> tuple[list[tuple[str, str | None, model.Submodel]], list[model.ConceptDescription]]:
    if not REFERENCE_AASX.is_file():
        raise SystemExit(f"reference AASX not found: {REFERENCE_AASX}")
    with zipfile.ZipFile(REFERENCE_AASX) as package:
        root = ET.fromstring(package.read(XML_NAME))
    submodels: list[tuple[str, str | None, model.Submodel]] = []
    for source in root.findall("aas:submodels/aas:submodel", NS):
        name = _text(source, "idShort")
        semantic = _semantic_of(source)
        submodel = model.Submodel(
            id_=f"urn:physical-ai:st-mini:submodel:{name}",
            id_short=name,
            display_name=_ml_name(*SUBMODEL_LABELS[name]),
            semantic_id=_semantic(*semantic) if semantic else None,
            administration=model.AdministrativeInformation(
                version="0",
                revision="1",
                template_id=semantic[1] if semantic else None,
            ),
            kind=model.ModellingKind.INSTANCE,
            submodel_element=[],
        )
        for child in _children(source, "aas:submodelElements"):
            submodel.submodel_element.add(_build_element(child))
        submodels.append((name, semantic[1] if semantic else None, submodel))

    concepts: list[model.ConceptDescription] = []
    seen: set[str] = set()
    for source in root.findall("aas:conceptDescriptions/aas:conceptDescription", NS):
        concept_id = _text(source, "identification")
        if not concept_id or concept_id in seen:
            continue
        names: dict[str, str] = {}
        for item in source.findall(".//IEC:preferredName/IEC:langString", NS):
            lang = item.get("lang") or "en"
            text = (item.text or "").strip()
            if text:
                names[lang] = text[:255]
        if not names:
            names["en"] = (_text(source, "idShort") or "Concept")[:255]
        short_name = None
        short = source.find(".//IEC:shortName/IEC:langString", NS)
        if short is not None and short.text and len(short.text.strip()) <= 18:
            short_name = model.ShortNameTypeIEC61360({short.get("lang") or "en": short.text.strip()})
        definition = None
        defined = source.find(".//IEC:definition/IEC:langString", NS)
        if defined is not None and defined.text and defined.text.strip():
            definition = model.DefinitionTypeIEC61360(
                {defined.get("lang") or "en": defined.text.strip()[:1023]}
            )
        data_type = None
        raw_type = source.findtext(".//IEC:dataType", default="", namespaces=NS).strip()
        if raw_type:
            try:
                data_type = model.DataTypeIEC61360[raw_type.strip().upper().replace("-", "_")]
            except KeyError:
                data_type = None
        unit = source.findtext(".//IEC:unit", default="", namespaces=NS).strip() or None
        content = model.DataSpecificationIEC61360(
            preferred_name=model.PreferredNameTypeIEC61360(names),
            short_name=short_name,
            definition=definition,
            data_type=data_type,
            unit=unit,
        )
        concepts.append(
            model.ConceptDescription(
                id_=concept_id,
                id_short=_sanitize(_text(source, "idShort")) or None,
                embedded_data_specifications=[
                    model.EmbeddedDataSpecification(
                        data_specification=_semantic("GlobalReference", IEC61360),
                        data_specification_content=content,
                    )
                ],
            )
        )
        seen.add(concept_id)
    return submodels, concepts


def _index(submodels: list[model.Submodel]) -> dict[str, model.Referable]:
    found: dict[str, model.Referable] = {}

    def walk(element: model.Referable, path: str) -> None:
        found[path] = element
        children = ()
        if isinstance(element, model.Submodel):
            children = element.submodel_element
        elif isinstance(element, model.SubmodelElementCollection):
            children = element.value
        for child in children:
            walk(child, f"{path}/{child.id_short}")

    for submodel in submodels:
        walk(submodel, submodel.id_short)
    return found


def _put(
    index: dict[str, model.Referable],
    path: str,
    value=_UNSET,
    ko: str = "",
    en: str = "",
) -> None:
    element = index[path]
    if value is not _UNSET:
        element.value = value
    if ko or en:
        element.description = _ml_text(ko or en, en or ko)


def _prop(
    id_short: str,
    value_type,
    value,
    ko: str,
    en: str,
    category: str | None = None,
) -> model.Property:
    return model.Property(
        id_short,
        value_type,
        value=value,
        display_name=_ml_name(ko, en),
        description=_ml_text(ko, en),
        category=category,
    )


def _fill(index: dict[str, model.Referable]) -> None:
    type_text = (
        "CAMTIC ST-mini. AgileX SCOUT MINI 베이스, Jetson AGX Orin, RPLidar S3, MW-AHRS, RealSense. "
        "로봇 PC는 Ubuntu 20.04, JetPack 5.1.3이고 ROS 2 Humble은 Docker 안에서 돈다."
    )
    _put(
        index,
        "Identification/ManufacturerName",
        "AgileX Robotics",
        "베이스 제조사. 통합 시스템 이름은 CAMTIC ST-mini 다.",
        "Chassis manufacturer. The integrated system is CAMTIC ST-mini.",
    )
    _put(index, "Identification/ManufacturerTypName", "SCOUT MINI", "베이스 형번.", "Chassis model.")
    _put(
        index,
        "Identification/ManufacturerTypDescription",
        type_text,
        "매뉴얼 v2.0에 적힌 구성.",
        "Configuration stated in manual v2.0.",
    )
    _put(
        index,
        "Identification/SupplierName",
        "캠틱종합기술원",
        "통합 시스템을 두는 쪽. 매뉴얼 작성자는 넥스루트 주식회사다.",
        "Organization for the integrated system. Nexroute wrote the manual.",
    )
    _put(index, "Identification/SupplierTypName", "CAMTIC ST-mini", "통합 시스템 이름.", "Integrated system name.")
    _put(
        index,
        "Identification/SupplierTypDescription",
        "사용 매뉴얼 v2.0은 넥스루트 주식회사가 2026-11-05에 작성했다.",
        "Operating manual v2.0 was written by Nexroute on 2026-11-05.",
    )
    _put(index, "Identification/TypClass", "AMR", "차체 분류.", "Asset class.")
    _put(index, "Identification/AssetId", ASSET_ID, "타입 식별자. 시리얼은 없다.", "Type identifier. No serial is recorded.")
    _put(
        index,
        "Identification/URL",
        "https://global.agilex.ai/products/scout-mini",
        "AgileX 제품 페이지.",
        "AgileX product page.",
    )
    _put(
        index,
        "Identification/ContactInfo01/Name",
        "넥스루트 주식회사",
        "매뉴얼 작성자.",
        "Manual author.",
    )
    _put(index, "Identification/ContactInfo01/Role", "매뉴얼 작성", "사용 매뉴얼 v2.0 작성.", "Author of operating manual v2.0.")
    _put(
        index,
        "Identification/ContactInfo01/URL",
        "https://github.com/CAMTIC-DATA-SKILL/SCOUT-mini_AMR_control",
        "ST-mini 제어 저장소.",
        "ST-mini control repository.",
    )
    _put(
        index,
        "Identification/SoftwareRevision",
        ko="ROS 2 Humble은 문자 버전이라 이 정수 속성에 넣지 않는다.",
        en="ROS 2 Humble is a text version, so it is not stored in this integer.",
    )
    _put(
        index,
        "Identification/Components/SteerableDrive",
        ko="원본 이름은 SteerableDrive 다. SCOUT MINI는 조향 바퀴가 아니라 4륜 차동 구동이고 제자리 회전을 한다.",
        en="The reference name is SteerableDrive. SCOUT MINI is four-wheel differential drive with in-place rotation, not a steered wheel.",
    )
    _put(
        index,
        "Identification/Components/SteerableDrive/MotorDrive_n/ManufacturerName",
        "AgileX Robotics",
        "허브 모터는 베이스에 포함된다. 모터 제조사 명판은 확인하지 않았다.",
        "The hub motors are part of the base. A separate motor nameplate was not checked.",
    )
    _put(
        index,
        "Identification/Components/SteerableDrive/MotorDrive_n/ModelName",
        "4x brushless hub motor",
        "영문 설명서 표는 4 x 150 W, 중문 소개는 4 x 250 W다.",
        "The English manual table says 4 x 150 W. The Chinese introduction says 4 x 250 W.",
    )
    _put(
        index,
        "Identification/Components/Battery/ManufacturerName",
        "AgileX Robotics",
        "배터리는 베이스 기본 탑재품이다.",
        "The battery is supplied with the base.",
    )
    _put(
        index,
        "Identification/Components/Battery/ModelName",
        "Lithium 24 V 15 Ah",
        "영문 설명서 성능 표.",
        "English user-manual performance table.",
    )
    _put(
        index,
        "Identification/Components/MainController/ManufacturerName",
        "NVIDIA",
        "온보드 PC. 베이스 CAN 제어기와는 별도다.",
        "Onboard PC, separate from the base CAN controller.",
    )
    _put(index, "Identification/Components/MainController/ModelName", "Jetson AGX Orin", "매뉴얼 2장.", "Manual section 2.")
    _put(
        index,
        "Identification/Components/Sensors/LaserSensor/LaserSensor_n/ManufacturerName",
        "Slamtec",
        "RPLidar 브랜드.",
        "RPLidar brand.",
    )
    _put(
        index,
        "Identification/Components/Sensors/LaserSensor/LaserSensor_n/ModelName",
        "RPLidar S3",
        "USB /dev/rplidar. 토픽 /scan. 안전 스캐너는 아니다.",
        "USB /dev/rplidar. Topic /scan. This is not a safety scanner.",
    )

    _put(index, "Nameplate/ManufacturerName", "AgileX Robotics", "베이스 제조사.", "Chassis manufacturer.")
    _put(index, "Nameplate/ManufacturerTypName", "SCOUT MINI", "베이스 형번.", "Chassis model.")
    _put(index, "Nameplate/TypClass", "AMR", "차체 분류.", "Asset class.")
    _put(
        index,
        "Nameplate/CountryOfOrigin",
        ko="확인한 문서에 원산지 표기가 없다.",
        en="The checked documents do not state a country of origin.",
    )
    _put(
        index,
        "Nameplate/SerialNo",
        ko="시리얼은 매뉴얼과 저장소에 없다.",
        en="No serial number is in the manual or the repository.",
    )

    _put(index, "TechnicalData/GeneralInformation/ManufacturerName", "AgileX Robotics", "베이스 제조사.", "Chassis manufacturer.")
    _put(
        index,
        "TechnicalData/GeneralInformation/ManufacturerProductDesignation",
        "SCOUT MINI",
        "베이스 형번. 통합 이름은 CAMTIC ST-mini.",
        "Chassis model. The integrated name is CAMTIC ST-mini.",
    )
    _put(
        index,
        "TechnicalData/ProductClassifications/ProductClassificationItem101/ProductClassificationSystem",
        "CAMTIC",
        "ECLASS 코드는 붙이지 않았다.",
        "No ECLASS code is assigned.",
    )
    _put(
        index,
        "TechnicalData/ProductClassifications/ProductClassificationItem101/ProductClassId",
        "AMR",
        "내부 분류.",
        "Internal class.",
    )

    general = "TechnicalData/TechnicalProperties/GeneralData"
    _put(
        index,
        f"{general}/LoadCapacity",
        10.0,
        "kg. 현재 AgileX 제품 페이지와 중문 소개의 정격. 영문 설명서 안전 문구는 20 kg을 넘기지 말라고 한다.",
        "kg. Rated load on the current AgileX product page and the Chinese introduction. The English manual says not to exceed 20 kg.",
    )
    _put(
        index,
        f"{general}/RatedSpeed",
        90,
        "컨셉 단위는 m/min. 90 m/min은 제품 페이지의 1.5 m/s다. 설명서 속도 모드는 0.5, 1.5, 3.0 m/s이고 표의 최고 속도는 10 km/h다.",
        "The concept unit is m/min. 90 m/min is the product page value of 1.5 m/s. Manual speed modes are 0.5, 1.5 and 3.0 m/s, and the table lists 10 km/h.",
    )
    _put(
        index,
        f"{general}/StandardWorkingTime",
        1,
        "단위 h. 제품 페이지의 만재 연속 운전. 설명서 표는 최대 8 h, 주행 10 km다.",
        "Hours. Full-load runtime on the product page. The manual table lists 8 h and 10 km.",
    )
    _put(index, f"{general}/BatteryType", "Lithium 24 V 15 Ah", "영문 설명서 성능 표.", "English user-manual performance table.")
    _put(
        index,
        f"{general}/Weight",
        23.0,
        "kg. 설명서 표의 kerb weight와 제품 페이지. 한 유통 문서는 26 kg으로 적는다.",
        "kg. Kerb weight in the manual table and on the product page. One distributor sheet lists 26 kg.",
    )
    _put(
        index,
        f"{general}/NabigationSystem",
        "Nav2, AMCL, slam_toolbox, RPLidar S3, wheel odometry",
        "원본 idShort 오타 NabigationSystem 을 유지한다. 내비게이션은 베이스 기본 기능이 아니라 ST-mini 소프트웨어다.",
        "The reference idShort typo NabigationSystem is kept. Navigation is ST-mini software, not a factory base function.",
    )
    _put(
        index,
        f"{general}/TurningRadius",
        0.0,
        "m. 설명서의 최소 회전반경 0. 컨셉 설명에 단위가 없어도 값은 0이다.",
        "m. Minimum turning radius 0 in the manual. The value is 0 even though the concept states no unit.",
    )
    _put(index, f"{general}/VehicleSize", "612 x 580 x 245 mm", "설명서 표와 제품 페이지가 같다.", "The manual table and the product page agree.")
    _put(
        index,
        f"{general}/ControllerType",
        "NVIDIA Jetson AGX Orin; chassis CAN",
        "온보드 PC와 베이스 CAN 제어를 함께 적는다.",
        "Records the onboard PC and the base CAN control together.",
    )
    _put(index, f"{general}/NumberOfWheel", 4, "구동륜 4개. 캐스터는 없다.", "Four driven wheels. There are no casters.")
    _put(
        index,
        f"{general}/TravelingDirection",
        "전진, 후진, 제자리 회전",
        "4륜 차동. 메카넘 전방향은 이 구성이 아니다.",
        "Four-wheel differential. This configuration is not the mecanum omnidirectional variant.",
    )

    battery = "TechnicalData/TechnicalProperties/GeneralData/Components/Battery"
    _put(index, f"{battery}/BatteryType", "Lithium", "설명서 표.", "Manual table.")
    _put(index, f"{battery}/NominalVoltage", 24, "V. 설명서의 배터리 전압.", "V. Battery voltage in the manual.")
    _put(
        index,
        f"{battery}/BatteryCapacity",
        15.0,
        "Ah. 설명서의 15 Ah. 컨셉 설명에 단위가 없어 단위는 여기에 적는다.",
        "Ah. 15 Ah from the manual. The concept states no unit, so the unit is recorded here.",
    )
    motor = "TechnicalData/TechnicalProperties/GeneralData/Components/SteerableDrive/SteerableDriveWheel_n/Motor"
    _put(
        index,
        f"{motor}/PowerConsumption",
        150,
        "W. 영문 설명서 표의 모터 한 대. 4대. 중문 소개는 250 W다.",
        "W for one motor in the English manual table. There are four. The Chinese introduction says 250 W.",
    )
    _put(
        index,
        f"{motor}/RatedOperatingVoltage",
        24.0,
        "V. 시스템 배터리 전압. 모터 단독 정격 전압은 설명서에 없다.",
        "V. System battery voltage. The manual does not give a separate motor voltage.",
    )
    _put(
        index,
        "TechnicalData/TechnicalProperties/GeneralData/Components/SteerableDrive/SteerableDriveWheel_n/Gear",
        ko="감속비 1:4.3은 설명서에 있다. GearRatio가 정수라 4.3을 넣지 않았다. 엔코더는 Hall이다.",
        en="The manual states a reduction ratio of 1:4.3. GearRatio is an integer, so 4.3 is not stored. The encoder is Hall.",
    )

    statement = (
        "제품 페이지와 설명서가 다른 값은 각 속성에 적었다. "
        "IP22, 최저지상고 115 mm, 축거 451 mm, 윤거 490 mm는 설명서 표 기준이다. "
        "등판은 설명서 30°, 제품 페이지는 만재 20°다. "
        "장애물 넘기는 제품 페이지 40 mm이고 일부 데이터시트는 70 mm다. "
        "ST-mini 주행 가이드의 0.4 m/s, 1.7 rad/s, 목표 허용 0.08 m와 약 3°는 소프트웨어 설정이다. "
        "유선 주소는 매뉴얼 v2.0의 192.168.1.211이다. README와 SLAM 가이드에는 192.168.1.220, 192.168.0.211도 있다."
    )
    if len(statement) > 1023:
        raise SystemExit(f"TextStatement is {len(statement)} characters")
    _put(
        index,
        "TechnicalData/FurtherInformation/TextStatement01",
        statement,
        "속성 하나에 들어가지 않는 출처 차이.",
        "Source differences that do not fit a single property.",
    )


def _described(element: model.SubmodelElement, ko: str, en: str) -> model.SubmodelElement:
    element.description = _ml_text(ko, en)
    return element


def _live_double(id_short: str, ko: str, en: str, note_ko: str, note_en: str) -> model.Property:
    return _described(
        _prop(id_short, model.datatypes.Double, None, ko, en, "VARIABLE"),
        note_ko,
        note_en,
    )


def _live_int(id_short: str, ko: str, en: str, note_ko: str, note_en: str) -> model.Property:
    return _described(
        _prop(id_short, model.datatypes.Integer, None, ko, en, "VARIABLE"),
        note_ko,
        note_en,
    )


def _live_bool(id_short: str, ko: str, en: str, note_ko: str, note_en: str) -> model.Property:
    return _described(
        _prop(id_short, model.datatypes.Boolean, None, ko, en, "VARIABLE"),
        note_ko,
        note_en,
    )


def _collection(id_short: str, ko: str, en: str, note_ko: str, note_en: str, children: list) -> model.SubmodelElementCollection:
    return _described(
        model.SubmodelElementCollection(id_short, display_name=_ml_name(ko, en), value=children),
        note_ko,
        note_en,
    )


def _double_list(id_short: str, ko: str, en: str, note_ko: str, note_en: str) -> model.SubmodelElementList:
    return _described(
        model.SubmodelElementList(
            id_short,
            model.Property,
            value_type_list_element=model.datatypes.Double,
            display_name=_ml_name(ko, en),
            category="VARIABLE",
        ),
        note_ko,
        note_en,
    )


def _wheel_odometry() -> model.SubmodelElementCollection:
    return _collection(
        "WheelOdometry",
        "바퀴 오도메트리",
        "Wheel odometry",
        "토픽 /wheel_odom. 지도 좌표와 달리 m, rad를 그대로 둔다. 수집 주소는 odom_x, odom_y, odom_yaw.",
        "Topic /wheel_odom. Unlike the map coordinates, meters and radians are kept. Collector addresses are odom_x, odom_y, and odom_yaw.",
        [
            _live_double("X", "X", "X", "m. 수집 주소 odom_x.", "m. Collector address odom_x."),
            _live_double("Y", "Y", "Y", "m. 수집 주소 odom_y.", "m. Collector address odom_y."),
            _live_double("Yaw", "요", "Yaw", "rad. 수집 주소 odom_yaw.", "rad. Collector address odom_yaw."),
        ],
    )


def _actuator_states() -> model.SubmodelElementCollection:
    wheel = _collection(
        "Wheel_n",
        "휠 n",
        "Wheel n",
        "메시지 슬롯 이름을 그대로 쓴다. n은 actuator_states 배열 인덱스다. 수집기 테스트는 8개까지 펼치지만, 실차 /scout_status를 보기 전에는 개수를 정하지 않는다. 카탈로그 구동륜은 4개다.",
        "Message slot names are kept. n is the actuator_states array index. The collector test unfolds eight entries, but the count stays unset until the live /scout_status is seen. The catalog has four driven wheels.",
        [
            _live_double("rpm", "회전수", "rpm", "수집 주소 actuator_states_{n}_rpm. 단위는 메시지에 따로 적지 않았다.", "Collector address actuator_states_{n}_rpm. The message note does not state a unit."),
            _live_double("current", "전류", "current", "수집 주소 actuator_states_{n}_current.", "Collector address actuator_states_{n}_current."),
            _live_double("temperature", "온도", "temperature", "수집 주소 actuator_states_{n}_temperature.", "Collector address actuator_states_{n}_temperature."),
        ],
    )
    return _collection(
        "ActuatorStates",
        "구동기 상태",
        "Actuator states",
        "원본 템플릿의 _n 반복 자리와 같이 휠 하나를 본보기로 둔다.",
        "One wheel exemplar, in the same way the reference template uses an _n slot.",
        [wheel],
    )


def _laser_scan() -> model.SubmodelElementCollection:
    limits = [
        ("AngleMin", "최소 각도", "Minimum angle", "rad", "scan_angle_min"),
        ("AngleMax", "최대 각도", "Maximum angle", "rad", "scan_angle_max"),
        ("AngleIncrement", "각도 증분", "Angle increment", "rad", "scan_angle_increment"),
        ("TimeIncrement", "시간 증분", "Time increment", "s", "scan_time_increment"),
        ("ScanTime", "스캔 시간", "Scan time", "s", "scan_scan_time"),
        ("RangeMin", "최소 거리", "Minimum range", "m", "scan_range_min"),
        ("RangeMax", "최대 거리", "Maximum range", "m", "scan_range_max"),
    ]
    children: list[model.SubmodelElement] = [
        _live_double(name, ko, en, f"{unit}. 수집 주소 {addr}. 토픽 /scan.", f"{unit}. Collector address {addr} on /scan.")
        for name, ko, en, unit, addr in limits
    ]
    children.append(
        _double_list(
            "Ranges",
            "거리 배열",
            "Ranges",
            "m. 수집 주소 scan_ranges. 점 수가 많아 템플릿에는 원소를 넣지 않는다. LaserSensor_n/OutputData1 문자열과는 별개다.",
            "m. Collector address scan_ranges. The template stores no elements because a scan has many points. This is separate from the LaserSensor_n/OutputData1 string.",
        )
    )
    children.append(
        _double_list(
            "Intensities",
            "강도 배열",
            "Intensities",
            "수집 주소 scan_intensities. 단위는 메시지에 없다. 템플릿에는 원소를 넣지 않는다.",
            "Collector address scan_intensities. The message note states no unit. The template stores no elements.",
        )
    )
    return _collection(
        "LaserScan",
        "라이다 스캔",
        "Laser scan",
        "토픽 /scan의 한계값과 배열. 점군을 OutputData1 문자열에 넣지 않는다.",
        "Limits and arrays from /scan. The point cloud is not stored in the OutputData1 string.",
        children,
    )


def _extend(index: dict[str, model.Referable]) -> None:
    sensors = index["Identification/Components/Sensors"]
    sensors.value.add(
        model.SubmodelElementCollection(
            "Imu",
            display_name=_ml_name("IMU", "IMU"),
            description=_ml_text(
                "원본 템플릿에 IMU 자리가 없어 추가했다. USB /dev/mw_ahrs.",
                "Added because the reference template has no IMU slot. USB /dev/mw_ahrs.",
            ),
            value=[
                _prop("ManufacturerName", model.datatypes.String, None, "제조사", "Manufacturer", "PARAMETER"),
                _prop("ModelName", model.datatypes.String, "MW-AHRS", "모델", "Model", "PARAMETER"),
                _prop("ProductCode", model.datatypes.String, None, "제품 코드", "Product code", "PARAMETER"),
            ],
        )
    )
    sensors.value.add(
        model.SubmodelElementCollection(
            "Camera",
            display_name=_ml_name("카메라", "Camera"),
            description=_ml_text(
                "원본 템플릿에 카메라 자리가 없어 추가했다. 매뉴얼은 RealSense까지만 적고 세부 모델은 없다.",
                "Added because the reference template has no camera slot. The manual names RealSense and no specific model.",
            ),
            value=[
                _prop("ManufacturerName", model.datatypes.String, "Intel", "제조사", "Manufacturer", "PARAMETER"),
                _prop("ModelName", model.datatypes.String, "RealSense", "모델", "Model", "PARAMETER"),
                _prop("ProductCode", model.datatypes.String, None, "제품 코드", "Product code", "PARAMETER"),
            ],
        )
    )
    operational = index["OperationalData"]
    voltage = _prop(
        "BatteryVoltage",
        model.datatypes.Double,
        None,
        "배터리 전압",
        "Battery voltage",
        "VARIABLE",
    )
    voltage.description = _ml_text(
        "V. /scout_status 가 보여주는 전압. CurrentBatteryCapacity는 %라 전압을 넣지 않는다.",
        "V. Voltage shown by /scout_status. CurrentBatteryCapacity is percent, so voltage is not stored there.",
    )
    operational.submodel_element.add(voltage)
    operational.submodel_element.add(_wheel_odometry())
    operational.submodel_element.add(
        _live_double(
            "AngularSpeed",
            "각속도",
            "Angular speed",
            "rad/s. 수집 주소 odom_wz, 토픽 /wheel_odom. /scout_status angular_velocity는 겹치므로 연결하지 않는다.",
            "rad/s. Collector address odom_wz on /wheel_odom. /scout_status angular_velocity is not connected because it duplicates this signal.",
        )
    )
    operational.submodel_element.add(
        _live_int(
            "ControlMode",
            "제어 모드",
            "Control mode",
            "수집 주소 control_mode, 토픽 /scout_status. 정수 코드의 열거 값은 실차 메시지를 보기 전에는 적지 않는다.",
            "Collector address control_mode on /scout_status. The integer enum is not recorded until the live message is seen.",
        )
    )
    operational.submodel_element.add(
        _live_bool(
            "LightControlEnabled",
            "조명 제어",
            "Light control enabled",
            "수집 주소 light_control_enabled, 토픽 /scout_status.",
            "Collector address light_control_enabled on /scout_status.",
        )
    )
    operational.submodel_element.add(
        _live_int(
            "FrontLightMode",
            "전방 조명 모드",
            "Front light mode",
            "수집 주소 front_light_state_mode, 토픽 /scout_status.",
            "Collector address front_light_state_mode on /scout_status.",
        )
    )
    operational.submodel_element.add(
        _live_int(
            "RearLightMode",
            "후방 조명 모드",
            "Rear light mode",
            "수집 주소 rear_light_state_mode, 토픽 /scout_status.",
            "Collector address rear_light_state_mode on /scout_status.",
        )
    )
    operational.submodel_element.add(_actuator_states())
    operational.submodel_element.add(_laser_scan())
    index["OperationalData"].description = _ml_text(
        "실시간 값은 비어 있다. 지도 좌표 X Y의 컨셉 단위는 mm이고 소스 amcl_x, amcl_y는 m다. 바퀴 오도메트리는 지도 좌표와 다른 프레임이라 m를 유지한다.",
        "Live values are empty. Map coordinates X and Y use the concept unit mm, while amcl_x and amcl_y are meters. Wheel odometry stays in meters because it is a different frame.",
    )
    index["Safety"].description = _ml_text(
        "비상정지와 보호정지 자리만 남겨 두었다. 매뉴얼은 서보 브레이크와 방호관을 안전 기능 목록으로 적지 않는다.",
        "Emergency-stop and protective-stop slots are kept empty. The manual does not list the servo brake or bumper tube as safety functions.",
    )
    index["Alarm"].description = _ml_text(
        "error_code 목록은 매뉴얼에 없다. 0이 정상이라는 것만 확인된다.",
        "The manual has no error_code catalog. Only error_code 0 meaning normal is confirmed.",
    )
    index["CAD"].description = _ml_text("CAD 파일은 이 패키지에 없다.", "This package has no CAD file.")
    index["ManufacturerDocumentation"].description = _ml_text(
        "원본 서브모델은 비어 있었다. 통합 설명은 README로 붙였다. AgileX 설명서 PDF는 넣지 않았다.",
        "The reference submodel was empty. The integration note is attached as README. The AgileX manual PDF is not embedded.",
    )


def _mapping(
    name: str,
    logical: str,
    raw: str,
    target: str,
    value_type: str,
    unit: str,
    policy: str,
    interval: int | None,
    storage: str,
    status: str,
    notes: str = "",
) -> model.SubmodelElementCollection:
    fields = [
        _prop("LogicalField", model.datatypes.String, logical, "논리 항목", "Logical field"),
        _prop("RawSourceField", model.datatypes.String, raw, "원천 필드", "Raw source field"),
        _prop("TargetAASPath", model.datatypes.String, target, "AAS 경로", "AAS path"),
        _prop("ValueType", model.datatypes.String, value_type, "값 형식", "Value type"),
        _prop("Unit", model.datatypes.String, unit, "단위", "Unit"),
        _prop("SamplingPolicy", model.datatypes.String, policy, "수집 방식", "Sampling policy"),
        _prop(
            "SamplingIntervalMs",
            model.datatypes.Int,
            interval,
            "수집 주기 ms",
            "Sampling interval ms",
        ),
        _prop("StorageTarget", model.datatypes.String, storage, "저장 대상", "Storage target"),
        _prop(
            "OPCUANodeId",
            model.datatypes.String,
            f"ns=2;s=ST_mini/{target}",
            "OPC UA 노드",
            "OPC UA node",
        ),
        _prop("VerificationStatus", model.datatypes.String, status, "확인 상태", "Verification status"),
    ]
    if notes:
        fields.append(_prop("Notes", model.datatypes.String, notes, "비고", "Notes"))
    return model.SubmodelElementCollection(name, display_name=_ml_name(logical, name), value=fields)


def _extra_submodels() -> list[model.Submodel]:
    readme_path = "/aasx/suppl/README.txt"
    csv_path = "/aasx/suppl/mapping.csv"
    report_path = "/aasx/suppl/prevalidation.md"
    maps = [
        _mapping(
            "MapErrorState",
            "베이스 오류 코드",
            "error_code. /scout_status. 매뉴얼에서 0은 정상.",
            "OperationalData/ErrorState",
            "xs:integer",
            "-",
            "CHANGE_BASED",
            None,
            "EventDB",
            "TESTBED_PROGRAM",
            "다른 코드 값은 매뉴얼에 없다.",
        ),
        _mapping(
            "MapBatteryVoltage",
            "배터리 전압",
            "battery_voltage. /scout_status.",
            "OperationalData/BatteryVoltage",
            "xs:double",
            "V",
            "FIXED_RATE",
            None,
            "TSDB",
            "PARTIAL",
            "주기는 매뉴얼에 없다.",
        ),
        _mapping(
            "MapBatteryCapacity",
            "배터리 잔량",
            "매뉴얼에 SOC 토픽이 없다.",
            "OperationalData/CurrentBatteryCapacity",
            "xs:double",
            "%",
            "FIXED_RATE",
            None,
            "TSDB",
            "NOT_IN_SOURCE",
        ),
        _mapping(
            "MapPoseX",
            "지도 X",
            "amcl_x. /amcl_pose position.x. 단위 m.",
            "OperationalData/Coordinates/X",
            "xs:double",
            "mm",
            "FIXED_RATE",
            None,
            "TSDB",
            "PARTIAL",
            "AAS 컨셉 단위는 mm다. 수집기가 m를 mm로 바꿔야 한다. 주기는 매뉴얼에 없다.",
        ),
        _mapping(
            "MapPoseY",
            "지도 Y",
            "amcl_y. /amcl_pose position.y. 단위 m.",
            "OperationalData/Coordinates/Y",
            "xs:double",
            "mm",
            "FIXED_RATE",
            None,
            "TSDB",
            "PARTIAL",
            "AAS 컨셉 단위는 mm다.",
        ),
        _mapping(
            "MapPoseAngle",
            "지도 방향",
            "amcl_yaw. /amcl_pose orientation 사원수의 yaw.",
            "OperationalData/Coordinates/Angle",
            "xs:double",
            "rad",
            "FIXED_RATE",
            None,
            "TSDB",
            "PARTIAL",
            "원본 컨셉은 Angle 단위를 정하지 않는다. ROS yaw는 rad다.",
        ),
        _mapping(
            "MapCurrentSpeed",
            "주행 속도",
            "odom_vx. /wheel_odom twist.linear.x. 약 50 Hz. 단위 m/s.",
            "OperationalData/CurrentSpeed",
            "xs:double",
            "m/min",
            "FIXED_RATE",
            20,
            "TSDB",
            "PARTIAL",
            "AAS 컨셉 단위는 m/min이다. linear_velocity는 이 속성과 겹쳐 연결하지 않는다. 각속도는 AngularSpeed의 odom_wz다.",
        ),
        _mapping(
            "MapLaserScan",
            "라이다 스캔",
            "/scan. 약 10 Hz. sensor_msgs/LaserScan.",
            "OperationalData/SensorData/LaserSensor/LaserSensor_n/OutputData1",
            "xs:string",
            "-",
            "FIXED_RATE",
            100,
            "TSDB",
            "PARTIAL",
            "점군은 LaserScan/Ranges, Intensities에 둔다. 이 문자열에는 넣지 않는다.",
        ),
        _mapping(
            "MapVehicleHeight",
            "차량 높이",
            "리프트가 없다. 차체 높이 245 mm는 TechnicalData VehicleSize에 있다.",
            "OperationalData/CurrentVehicleHeight",
            "xs:double",
            "mm",
            "FIXED_RATE",
            None,
            "TSDB",
            "NOT_IN_SOURCE",
        ),
        _mapping(
            "MapLocationName",
            "위치 이름",
            "장소 이름 토픽은 매뉴얼에 없다.",
            "OperationalData/CurrentLocation",
            "xs:string",
            "-",
            "CHANGE_BASED",
            None,
            "EventDB",
            "NOT_IN_SOURCE",
        ),
        _mapping(
            "MapOperationalState",
            "운전 상태",
            "vehicle_state. /scout_status.",
            "OperationalData/OperationalState",
            "xs:integer",
            "-",
            "CHANGE_BASED",
            None,
            "EventDB",
            "TESTBED_PROGRAM",
            "정수 코드의 열거 값은 실차 메시지를 보기 전에는 적지 않는다.",
        ),
        _mapping(
            "MapOdomX",
            "바퀴 오도메트리 X",
            "odom_x. /wheel_odom. 약 50 Hz. 단위 m.",
            "OperationalData/WheelOdometry/X",
            "xs:double",
            "m",
            "FIXED_RATE",
            20,
            "TSDB",
            "TESTBED_PROGRAM",
            "지도 좌표와 달리 mm로 바꾸지 않는다.",
        ),
        _mapping(
            "MapOdomY",
            "바퀴 오도메트리 Y",
            "odom_y. /wheel_odom. 약 50 Hz. 단위 m.",
            "OperationalData/WheelOdometry/Y",
            "xs:double",
            "m",
            "FIXED_RATE",
            20,
            "TSDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapOdomYaw",
            "바퀴 오도메트리 방향",
            "odom_yaw. /wheel_odom. 약 50 Hz. 단위 rad.",
            "OperationalData/WheelOdometry/Yaw",
            "xs:double",
            "rad",
            "FIXED_RATE",
            20,
            "TSDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapAngularSpeed",
            "각속도",
            "odom_wz. /wheel_odom. 약 50 Hz. 단위 rad/s.",
            "OperationalData/AngularSpeed",
            "xs:double",
            "rad/s",
            "FIXED_RATE",
            20,
            "TSDB",
            "TESTBED_PROGRAM",
            "/scout_status angular_velocity는 연결하지 않는다.",
        ),
        _mapping(
            "MapControlMode",
            "제어 모드",
            "control_mode. /scout_status.",
            "OperationalData/ControlMode",
            "xs:integer",
            "-",
            "CHANGE_BASED",
            None,
            "EventDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapLightControlEnabled",
            "조명 제어",
            "light_control_enabled. /scout_status.",
            "OperationalData/LightControlEnabled",
            "xs:boolean",
            "-",
            "CHANGE_BASED",
            None,
            "EventDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapFrontLightMode",
            "전방 조명 모드",
            "front_light_state_mode. /scout_status.",
            "OperationalData/FrontLightMode",
            "xs:integer",
            "-",
            "CHANGE_BASED",
            None,
            "EventDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapRearLightMode",
            "후방 조명 모드",
            "rear_light_state_mode. /scout_status.",
            "OperationalData/RearLightMode",
            "xs:integer",
            "-",
            "CHANGE_BASED",
            None,
            "EventDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapActuatorRpm",
            "휠 회전수",
            "actuator_states_{n}_rpm. /scout_status.",
            "OperationalData/ActuatorStates/Wheel_n/rpm",
            "xs:double",
            "-",
            "FIXED_RATE",
            None,
            "TSDB",
            "PARTIAL",
            "n은 배열 인덱스. 수집기 테스트는 0부터 7까지 펼친다. 실차 개수는 아직 정하지 않는다.",
        ),
        _mapping(
            "MapActuatorCurrent",
            "휠 전류",
            "actuator_states_{n}_current. /scout_status.",
            "OperationalData/ActuatorStates/Wheel_n/current",
            "xs:double",
            "-",
            "FIXED_RATE",
            None,
            "TSDB",
            "PARTIAL",
        ),
        _mapping(
            "MapActuatorTemperature",
            "휠 온도",
            "actuator_states_{n}_temperature. /scout_status.",
            "OperationalData/ActuatorStates/Wheel_n/temperature",
            "xs:double",
            "-",
            "FIXED_RATE",
            None,
            "TSDB",
            "PARTIAL",
        ),
        _mapping(
            "MapScanAngleMin",
            "스캔 최소 각도",
            "scan_angle_min. /scan. 약 10 Hz.",
            "OperationalData/LaserScan/AngleMin",
            "xs:double",
            "rad",
            "FIXED_RATE",
            100,
            "TSDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapScanAngleMax",
            "스캔 최대 각도",
            "scan_angle_max. /scan. 약 10 Hz.",
            "OperationalData/LaserScan/AngleMax",
            "xs:double",
            "rad",
            "FIXED_RATE",
            100,
            "TSDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapScanAngleIncrement",
            "스캔 각도 증분",
            "scan_angle_increment. /scan. 약 10 Hz.",
            "OperationalData/LaserScan/AngleIncrement",
            "xs:double",
            "rad",
            "FIXED_RATE",
            100,
            "TSDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapScanTimeIncrement",
            "스캔 시간 증분",
            "scan_time_increment. /scan. 약 10 Hz.",
            "OperationalData/LaserScan/TimeIncrement",
            "xs:double",
            "s",
            "FIXED_RATE",
            100,
            "TSDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapScanTime",
            "스캔 주기",
            "scan_scan_time. /scan. 약 10 Hz.",
            "OperationalData/LaserScan/ScanTime",
            "xs:double",
            "s",
            "FIXED_RATE",
            100,
            "TSDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapScanRangeMin",
            "스캔 최소 거리",
            "scan_range_min. /scan. 약 10 Hz.",
            "OperationalData/LaserScan/RangeMin",
            "xs:double",
            "m",
            "FIXED_RATE",
            100,
            "TSDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapScanRangeMax",
            "스캔 최대 거리",
            "scan_range_max. /scan. 약 10 Hz.",
            "OperationalData/LaserScan/RangeMax",
            "xs:double",
            "m",
            "FIXED_RATE",
            100,
            "TSDB",
            "TESTBED_PROGRAM",
        ),
        _mapping(
            "MapScanRanges",
            "스캔 거리 배열",
            "scan_ranges. /scan. 약 10 Hz.",
            "OperationalData/LaserScan/Ranges",
            "xs:double[]",
            "m",
            "FIXED_RATE",
            100,
            "TSDB",
            "PARTIAL",
            "점 수가 많다. 템플릿 리스트에는 원소가 없다.",
        ),
        _mapping(
            "MapScanIntensities",
            "스캔 강도 배열",
            "scan_intensities. /scan. 약 10 Hz.",
            "OperationalData/LaserScan/Intensities",
            "xs:double[]",
            "-",
            "FIXED_RATE",
            100,
            "TSDB",
            "PARTIAL",
            "점 수가 많다. 템플릿 리스트에는 원소가 없다.",
        ),
    ]
    acquisition = model.Submodel(
        "urn:physical-ai:st-mini:submodel:DataAcquisitionConfiguration",
        id_short="DataAcquisitionConfiguration",
        display_name=_ml_name(*SUBMODEL_LABELS["DataAcquisitionConfiguration"]),
        description=_ml_text(
            "HH7 템플릿과 같은 수집 설정. OPC UA 노드 문자열은 경로 자리다. 확인된 서버 주소가 아니다.",
            "Acquisition block in the same shape as the HH7 template. The OPC UA node string is a path placeholder, not a confirmed server.",
        ),
        administration=model.AdministrativeInformation(version="0", revision="1", template_id=TEMPLATE_ID),
        submodel_element=[
            model.SubmodelElementCollection(
                "SourceInterface",
                display_name=_ml_name("원천 인터페이스", "Source interface"),
                value=[
                    _prop("PhysicalInterface", model.datatypes.String, "로봇 PC 이더넷. 베이스는 CAN can0.", "물리 인터페이스", "Physical interface"),
                    _prop("ApplicationProtocol", model.datatypes.String, "ROS 2 Humble topic. 베이스 상태는 CAN.", "응용 프로토콜", "Application protocol"),
                    _prop("EdgeRole", model.datatypes.String, "ROS 2 토픽을 AAS 속성에 매핑", "엣지 역할", "Edge role"),
                    _prop("Endpoint", model.datatypes.String, "192.168.1.211", "접속 주소", "Endpoint"),
                ],
            ),
            model.SubmodelElementCollection(
                "Mappings",
                display_name=_ml_name("매핑", "Mappings"),
                value=maps,
            ),
        ],
    )
    historical = model.Submodel(
        "urn:physical-ai:st-mini:submodel:HistoricalDataConfiguration",
        id_short="HistoricalDataConfiguration",
        display_name=_ml_name(*SUBMODEL_LABELS["HistoricalDataConfiguration"]),
        description=_ml_text(
            "최신 값은 AAS 속성에 두고 이력은 시계열로 보낸다. HH7 템플릿의 이력 설정을 따랐다.",
            "Latest values stay on AAS properties and history goes to a time-series store. Follows the HH7 history block.",
        ),
        administration=model.AdministrativeInformation(version="0", revision="1", template_id=TEMPLATE_ID),
        submodel_element=[
            _prop("CurrentValuePolicy", model.datatypes.String, "AAS_PROPERTY_LATEST_VALUE_ONLY", "현재값 정책", "Current value policy"),
            _prop("HistoricalStorage", model.datatypes.String, "TSDB", "이력 저장", "Historical storage"),
            _prop("OperationalEventStorage", model.datatypes.String, "EventDB", "이벤트 저장", "Operational event storage"),
            _prop("EdgeBuffering", model.datatypes.Boolean, True, "엣지 버퍼", "Edge buffering"),
            _prop(
                "TimestampPolicy",
                model.datatypes.String,
                "SOURCE_TIMESTAMP_IF_AVAILABLE_ELSE_EDGE_RECEIVE_TIMESTAMP",
                "시각 정책",
                "Timestamp policy",
            ),
            _prop("QualityPolicy", model.datatypes.String, "OPCUA_STATUSCODE", "품질 정책", "Quality policy"),
            _prop("ClockSynchronization", model.datatypes.String, "NTP_OR_PTP_TBD", "시각 동기", "Clock synchronization"),
        ],
    )
    documentation = model.Submodel(
        "urn:physical-ai:st-mini:submodel:Documentation",
        id_short="Documentation",
        display_name=_ml_name(*SUBMODEL_LABELS["Documentation"]),
        administration=model.AdministrativeInformation(version="0", revision="1", template_id=TEMPLATE_ID),
        submodel_element=[
            model.File("TemplateReadme", "text/plain", readme_path, display_name=_ml_name("템플릿 설명", "Template readme")),
            model.File("MappingTable", "text/csv", csv_path, display_name=_ml_name("매핑 표", "Mapping table")),
            model.File("PreValidationReport", "text/markdown", report_path, display_name=_ml_name("사전검증", "Pre-validation report")),
            model.File(
                "DigitalTwinCadModel",
                "application/step",
                display_name=_ml_name("디지털 트윈 CAD 모델", "Digital Twin CAD Model"),
                semantic_id=_semantic("GlobalReference", "urn:physical-ai:ids/cd/DigitalTwinCadModel/1/0"),
            ),
            model.File(
                "DigitalTwinUsdModel",
                "model/vnd.usda+ascii",
                display_name=_ml_name("디지털 트윈 USD 모델", "Digital Twin USD Model"),
                semantic_id=_semantic("GlobalReference", "urn:physical-ai:ids/cd/DigitalTwinUsdModel/1/0"),
            ),
        ],
    )
    return [acquisition, historical, documentation]


def _documents() -> dict[str, str]:
    readme = """CAMTIC ST-mini AMR AAS

구조는 KOSMO Nestfield AGV-AMR AAS Template v1.0.0 의 여덟 서브모델이다.
Nameplate, Identification, TechnicalData, OperationalData,
ManufacturerDocumentation, Safety, Alarm, CAD.

수집과 이력, 문서 서브모델은 HH7 v0.3 템플릿과 같은 자리다.
IMU, Camera, BatteryVoltage 는 원본에 없어 추가했다.
수집기가 보내는 바퀴 오도메트리, 각속도, 제어·조명, 구동기 슬롯, 라이다 한계와 배열도 OperationalData에 추가했다.
idShort 안의 점은 뺐다. 뷰어가 경로를 점으로 나눈다.

베이스는 AgileX SCOUT MINI 다.
온보드 PC는 Jetson AGX Orin, Ubuntu 20.04, JetPack 5.1.3.
ROS 2 Humble은 camtic_dev 컨테이너 안에서 돈다.
센서는 RPLidar S3, MW-AHRS, RealSense.

카탈로그에서 숫자로 넣은 값
- 가반하중 10 kg
- 정격 속도 90 m/min (제품 페이지 1.5 m/s)
- 만재 연속 운전 1 h
- 질량 23 kg
- 크기 612 x 580 x 245 mm
- 바퀴 4개, 회전반경 0 m
- 배터리 24 V 15 Ah
- 모터 150 W 한 대, 4대

설명서와 제품 페이지가 다른 값은 TechnicalData/FurtherInformation 과 각 속성에 적었다.
실시간 값은 비어 있다.
바퀴 오도메트리 X Y는 m, 지도 좌표 X Y는 mm다.
휠 상태는 Wheel_n 하나다. 개수는 실차 /scout_status를 본 뒤에 정한다.
유선 주소는 매뉴얼 v2.0의 192.168.1.211 이다.
"""
    report = """# ST-mini 사전검증

채운 값
- 명판과 식별의 제조사, 형번, 통합 구성
- TechnicalData의 질량, 크기, 정격 속도, 가반하중, 배터리, 바퀴 수
- 매뉴얼에 적힌 ROS 토픽과 수집 주소를 DataAcquisitionConfiguration에 연결
- vehicle_state를 OperationalState에 연결
- WheelOdometry, AngularSpeed, ControlMode, 조명, ActuatorStates/Wheel_n, LaserScan

비운 값
- 좌표, 속도, 전압, 오류 코드와 새로 만든 속성의 실시간 값
- 시리얼, 원산지, 명판 마크, CAD
- CurrentBatteryCapacity, CurrentVehicleHeight, CurrentLocation, Source, Destination, StartUpTime, TotalPowerOnTime
- 안전 스캐너, 자기 가이드, 범퍼, 스토퍼, 장애물, 압력, 로드셀, 캐스터, 무선 라우터
- wheel_odom_ok, scout_status_ok, amcl_ok, scan_ok

출처가 갈리는 값
- 속도: 제품 페이지 1.5 m/s, 설명서 모드 최대 3.0 m/s, 표 10 km/h
- 가반하중: 제품 페이지 10 kg, 설명서 안전 문구 20 kg
- 운전 시간: 제품 페이지 만재 1 h, 설명서 표 8 h
- 등판: 설명서 30°, 제품 페이지 만재 20°
- 모터: 영문 표 150 W, 중문 소개 250 W
- 주소: 매뉴얼 v2.0 유선 192.168.1.211, README와 SLAM 가이드의 192.168.1.220 및 192.168.0.211

남은 확인
- /scout_status 주기, error_code가 0이 아닐 때의 의미, control_mode와 조명 모드의 열거 값
- actuator_states 실제 길이. 템플릿은 Wheel_n 하나만 둔다
- /amcl_pose 주기와 yaw 부호
- RealSense 세부 모델과 MW-AHRS 제조사
"""
    return {"readme": readme, "report": report}


def _mapping_rows(acquisition: model.Submodel) -> str:
    rows = ["logical_field,raw_source,unit,verification"]
    mappings = next(child for child in acquisition.submodel_element if child.id_short == "Mappings")
    for block in mappings.value:
        fields = {child.id_short: child.value for child in block.value}
        raw = str(fields.get("RawSourceField") or "").replace('"', "'")
        unit = str(fields.get("Unit") or "").replace('"', "'")
        rows.append(
            ",".join(
                [
                    block.id_short,
                    f'"{raw}"',
                    f'"{unit}"',
                    str(fields.get("VerificationStatus") or ""),
                ]
            )
        )
    return "\n".join(rows) + "\n"


def build() -> Path:
    loaded, concepts = _load_reference()
    by_name = {name: submodel for name, _, submodel in loaded}
    index = _index(list(by_name.values()))
    _fill(index)
    _extend(index)
    docs = _documents()
    extra = _extra_submodels()
    by_name["ManufacturerDocumentation"].submodel_element.add(
        model.File(
            "IntegrationManual",
            "text/plain",
            "/aasx/suppl/README.txt",
            display_name=_ml_name("통합 설명", "Integration note"),
        )
    )
    acquisition = next(item for item in extra if item.id_short == "DataAcquisitionConfiguration")
    csv_text = _mapping_rows(acquisition)
    files = aasx.DictSupplementaryFileContainer()
    files.add_file("/aasx/suppl/README.txt", io.BytesIO(docs["readme"].encode()), "text/plain")
    files.add_file("/aasx/suppl/mapping.csv", io.BytesIO(csv_text.encode()), "text/csv")
    files.add_file("/aasx/suppl/prevalidation.md", io.BytesIO(docs["report"].encode()), "text/markdown")

    for submodel in by_name.values():
        submodel.description = submodel.description or _ml_text(*SUBMODEL_LABELS[submodel.id_short])
    ordered = [by_name[name] for name in SUBMODEL_ORDER if name in by_name]
    ordered.extend(extra)
    shell = model.AssetAdministrationShell(
        asset_information=model.AssetInformation(
            asset_kind=model.AssetKind.TYPE,
            global_asset_id=ASSET_ID,
        ),
        id_=SHELL_ID,
        id_short="ST_mini",
        display_name=_ml_name("CAMTIC ST-mini AMR AAS", "CAMTIC ST-mini AMR AAS"),
        description=_ml_text(
            "AgileX SCOUT MINI 베이스의 CAMTIC ST-mini. 서브모델 구조는 KOSMO AGV-AMR AAS Template v1.0.0을 따른다. 실시간 값은 비어 있다.",
            "CAMTIC ST-mini on an AgileX SCOUT MINI base. Submodel structure follows the KOSMO AGV-AMR AAS Template v1.0.0. Live values are empty.",
        ),
        administration=model.AdministrativeInformation(version="0", revision="1", template_id=TEMPLATE_ID),
        submodel={model.ModelReference.from_referable(item) for item in ordered},
    )
    store = model.DictIdentifiableStore([shell, *ordered, *concepts])
    _check(store)
    dest = OUT_DIR / "package.aasx"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with aasx.AASXWriter(dest) as writer:
        writer.write_all_aas_objects("/aasx/data.json", store, files)
    return dest


def _check(store: model.DictIdentifiableStore) -> None:
    submodels = [obj for obj in store if isinstance(obj, model.Submodel)]
    names = [item.id_short for item in submodels]
    if names != list(SUBMODEL_ORDER):
        raise SystemExit(f"submodel order {names}")
    index = _index(submodels)
    expected = {
        "Identification/ManufacturerName": "AgileX Robotics",
        "TechnicalData/TechnicalProperties/GeneralData/LoadCapacity": 10.0,
        "TechnicalData/TechnicalProperties/GeneralData/RatedSpeed": 90,
        "TechnicalData/TechnicalProperties/GeneralData/Weight": 23.0,
        "TechnicalData/TechnicalProperties/GeneralData/NumberOfWheel": 4,
        "TechnicalData/TechnicalProperties/GeneralData/Components/Battery/NominalVoltage": 24,
        "Identification/Components/Sensors/Imu/ModelName": "MW-AHRS",
        "Identification/Components/Sensors/Camera/ModelName": "RealSense",
        "Identification/Components/Sensors/LaserSensor/LaserSensor_n/ModelName": "RPLidar S3",
        "DataAcquisitionConfiguration/Mappings/MapCurrentSpeed/SamplingIntervalMs": 20,
        "DataAcquisitionConfiguration/SourceInterface/Endpoint": "192.168.1.211",
        "DataAcquisitionConfiguration/Mappings/MapOperationalState/VerificationStatus": "TESTBED_PROGRAM",
        "DataAcquisitionConfiguration/Mappings/MapOdomX/Unit": "m",
        "DataAcquisitionConfiguration/Mappings/MapAngularSpeed/Unit": "rad/s",
    }
    for path, value in expected.items():
        if index[path].value != value:
            raise SystemExit(f"{path} = {index[path].value!r}")
    empty_paths = (
        "OperationalData/Coordinates/X",
        "OperationalData/ErrorState",
        "OperationalData/BatteryVoltage",
        "OperationalData/CurrentSpeed",
        "OperationalData/WheelOdometry/X",
        "OperationalData/WheelOdometry/Yaw",
        "OperationalData/AngularSpeed",
        "OperationalData/ControlMode",
        "OperationalData/LightControlEnabled",
        "OperationalData/ActuatorStates/Wheel_n/rpm",
        "OperationalData/LaserScan/AngleMin",
        "OperationalData/LaserScan/RangeMax",
    )
    for path in empty_paths:
        if index[path].value is not None:
            raise SystemExit(f"{path} should be empty")
    ranges = index["OperationalData/LaserScan/Ranges"]
    intensities = index["OperationalData/LaserScan/Intensities"]
    if not isinstance(ranges, model.SubmodelElementList) or len(list(ranges.value)) != 0:
        raise SystemExit("Ranges must be an empty double list")
    if ranges.value_type_list_element is not model.datatypes.Double:
        raise SystemExit("Ranges value type")
    if not isinstance(intensities, model.SubmodelElementList) or len(list(intensities.value)) != 0:
        raise SystemExit("Intensities must be an empty double list")
    if any("." in path.split("/")[-1] for path in index):
        raise SystemExit("dotted idShort remains")
    concepts = [obj for obj in store if isinstance(obj, model.ConceptDescription)]
    if len(concepts) < 100:
        raise SystemExit(f"only {len(concepts)} concept descriptions")


def main() -> None:
    path = build()
    print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
