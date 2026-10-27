"""AAS 패키지에서 OPC-UA 적용용 산출물(nodeset.xml, engineering.csv, syscfg.json)을 생성한다.

OPC UA Companion Specification for AAS (OPC 30270 / I4AAS)의 인스턴스 매핑 개념을 따르며,
AAS Shell → Object, Submodel → Object, Property류 → Variable 로 변환한다.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from xml.dom import minidom

from basyx.aas import model

from utils.log_setup import LogSetup

from .repository import AasxPackage

logger = LogSetup.get_logger(__name__)

ARTIFACT_NAMES = ("nodeset.xml", "engineering.csv", "syscfg.json")

# OPC UA / I4AAS 관련 네임스페이스
_NS_UA = "http://opcfoundation.org/UA/"
_NS_I4AAS = "http://opcfoundation.org/UA/I4AAS/"
_NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

_XSD_TO_OPC: dict[str, str] = {
    "xs:boolean": "Boolean",
    "xs:byte": "SByte",
    "xs:short": "Int16",
    "xs:int": "Int32",
    "xs:long": "Int64",
    "xs:unsignedByte": "Byte",
    "xs:unsignedShort": "UInt16",
    "xs:unsignedInt": "UInt32",
    "xs:unsignedLong": "UInt64",
    "xs:float": "Float",
    "xs:double": "Double",
    "xs:string": "String",
    "xs:date": "DateTime",
    "xs:dateTime": "DateTime",
    "xs:time": "String",
    "xs:duration": "String",
    "xs:anyURI": "String",
    "xs:base64Binary": "ByteString",
}


@dataclass
class _NodeRecord:
    node_id: str
    browse_name: str
    display_name: str
    node_class: str
    data_type: str
    parent_node_id: str
    aas_path: str
    aas_id: str
    model_type: str
    semantic_id: str
    value: str
    value_type: str


def _safe_browse_name(value: Optional[str], fallback: str) -> str:
    raw = (value or fallback).strip() or fallback
    cleaned = re.sub(r"[^\w\-./]", "_", raw, flags=re.UNICODE)
    return cleaned[:120] or fallback


def _namespace_uri(package: AasxPackage) -> str:
    digest = hashlib.sha1(package.id.encode("utf-8")).hexdigest()[:12]
    return f"https://camtic.or.kr/opcua/aas/{package.id}/{digest}"


def _reference_str(ref: Optional[model.Reference]) -> str:
    if ref is None or not ref.key:
        return ""
    return " / ".join(str(key.value) for key in ref.key)


def _xsd_name(type_: Optional[type]) -> str:
    if type_ is None:
        return ""
    return model.datatypes.XSD_TYPE_NAMES.get(type_, "") or ""


def _opc_data_type(xsd_name: str) -> str:
    return _XSD_TO_OPC.get(xsd_name, "String")


def _lang_preview(value: Optional[model.LangStringSet]) -> str:
    if not value:
        return ""
    for lang in ("ko", "en"):
        if lang in value:
            return value[lang]
    return next(iter(value.values()), "")


def _property_value(element: model.SubmodelElement) -> tuple[str, str]:
    if isinstance(element, model.Property):
        xsd = _xsd_name(element.value_type)
        return ("" if element.value is None else model.datatypes.xsd_repr(element.value), xsd)
    if isinstance(element, model.MultiLanguageProperty):
        return _lang_preview(element.value), "xs:string"
    if isinstance(element, model.Range):
        xsd = _xsd_name(element.value_type)
        lo = "" if element.min is None else model.datatypes.xsd_repr(element.min)
        hi = "" if element.max is None else model.datatypes.xsd_repr(element.max)
        return f"{lo}~{hi}", xsd
    if isinstance(element, model.File):
        return element.value or "", "xs:anyURI"
    if isinstance(element, model.Blob):
        size = len(element.value) if element.value else 0
        return f"{size} bytes", "xs:base64Binary"
    if isinstance(element, model.ReferenceElement):
        return _reference_str(element.value), "xs:string"
    return "", ""


def _walk_elements(
    elements: Iterable[model.SubmodelElement],
    parent_node_id: str,
    aas_path: str,
    start_ns: int,
) -> tuple[list[_NodeRecord], int]:
    records: list[_NodeRecord] = []
    ns = start_ns
    for element in elements:
        ns += 1
        browse = _safe_browse_name(element.id_short, f"Element_{ns}")
        path = f"{aas_path}/{browse}"
        node_id = f"ns=1;i={ns}"
        model_type = type(element).__name__
        value, value_type = _property_value(element)
        is_container = isinstance(
            element,
            (
                model.SubmodelElementCollection,
                model.SubmodelElementList,
                model.Entity,
                model.Operation,
                model.AnnotatedRelationshipElement,
            ),
        )
        node_class = "Object" if is_container else "Variable"
        data_type = "" if is_container else _opc_data_type(value_type or "xs:string")
        display = _lang_preview(element.display_name) or browse
        records.append(
            _NodeRecord(
                node_id=node_id,
                browse_name=browse,
                display_name=display,
                node_class=node_class,
                data_type=data_type,
                parent_node_id=parent_node_id,
                aas_path=path,
                aas_id="",
                model_type=model_type,
                semantic_id=_reference_str(getattr(element, "semantic_id", None)),
                value=value,
                value_type=value_type,
            )
        )
        children: list[model.SubmodelElement] = []
        if isinstance(element, model.SubmodelElementCollection):
            children = list(element.value)
        elif isinstance(element, model.SubmodelElementList):
            children = list(element.value)
        elif isinstance(element, model.Entity):
            children = list(element.statement)
        elif isinstance(element, model.AnnotatedRelationshipElement):
            children = list(element.annotation)
        elif isinstance(element, model.Operation):
            for variables in (
                element.input_variable,
                element.output_variable,
                element.in_output_variable,
            ):
                children.extend(variables)
        if children:
            child_records, ns = _walk_elements(children, node_id, path, ns)
            records.extend(child_records)
    return records, ns


def build_node_records(package: AasxPackage) -> list[_NodeRecord]:
    records: list[_NodeRecord] = []
    ns = 1000
    root_id = "ns=1;i=1000"
    records.append(
        _NodeRecord(
            node_id=root_id,
            browse_name=_safe_browse_name(package.id, "AASPackage"),
            display_name=package.path.stem,
            node_class="Object",
            data_type="",
            parent_node_id="i=85",  # Objects folder
            aas_path=f"/{package.id}",
            aas_id=package.id,
            model_type="AasxPackage",
            semantic_id="",
            value="",
            value_type="",
        )
    )

    for shell in package.shells:
        ns += 1
        shell_id = f"ns=1;i={ns}"
        browse = _safe_browse_name(shell.id_short, f"AAS_{ns}")
        shell_path = f"/{package.id}/{browse}"
        records.append(
            _NodeRecord(
                node_id=shell_id,
                browse_name=browse,
                display_name=_lang_preview(shell.display_name) or browse,
                node_class="Object",
                data_type="",
                parent_node_id=root_id,
                aas_path=shell_path,
                aas_id=shell.id,
                model_type="AssetAdministrationShell",
                semantic_id="",
                value=shell.asset_information.global_asset_id or "",
                value_type="",
            )
        )
        for ref in sorted(shell.submodel, key=lambda r: str(r.key[0].value)):
            submodel_id = str(ref.key[0].value)
            submodel = package.get_identifiable(submodel_id, model.Submodel)
            if submodel is None:
                continue
            ns += 1
            sm_node = f"ns=1;i={ns}"
            sm_browse = _safe_browse_name(submodel.id_short, f"SM_{ns}")
            sm_path = f"{shell_path}/{sm_browse}"
            records.append(
                _NodeRecord(
                    node_id=sm_node,
                    browse_name=sm_browse,
                    display_name=_lang_preview(submodel.display_name) or sm_browse,
                    node_class="Object",
                    data_type="",
                    parent_node_id=shell_id,
                    aas_path=sm_path,
                    aas_id=submodel.id,
                    model_type="Submodel",
                    semantic_id=_reference_str(submodel.semantic_id),
                    value="",
                    value_type="",
                )
            )
            child_records, ns = _walk_elements(submodel.submodel_element, sm_node, sm_path, ns)
            records.extend(child_records)
    return records


def _build_nodeset_xml(package: AasxPackage, records: list[_NodeRecord], namespace_uri: str) -> str:
    ET.register_namespace("", _NS_UA)
    ET.register_namespace("xsi", _NS_XSI)
    ET.register_namespace("uax", "http://opcfoundation.org/UA/2008/02/Types.xsd")

    root = ET.Element(
        f"{{{_NS_UA}}}UANodeSet",
        {
            f"{{{_NS_XSI}}}schemaLocation": (
                "http://opcfoundation.org/UA/ "
                "http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"
            ),
        },
    )
    ns_uris = ET.SubElement(root, f"{{{_NS_UA}}}NamespaceUris")
    uri_el = ET.SubElement(ns_uris, f"{{{_NS_UA}}}Uri")
    uri_el.text = namespace_uri
    i4aas = ET.SubElement(ns_uris, f"{{{_NS_UA}}}Uri")
    i4aas.text = _NS_I4AAS

    models = ET.SubElement(root, f"{{{_NS_UA}}}Models")
    model_el = ET.SubElement(
        models,
        f"{{{_NS_UA}}}Model",
        {
            "ModelUri": namespace_uri,
            "Version": "1.0.0",
            "PublicationDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    ET.SubElement(
        model_el,
        f"{{{_NS_UA}}}RequiredModel",
        {
            "ModelUri": _NS_UA,
            "Version": "1.04",
        },
    )
    ET.SubElement(
        model_el,
        f"{{{_NS_UA}}}RequiredModel",
        {
            "ModelUri": _NS_I4AAS,
            "Version": "1.0.0",
        },
    )

    aliases = ET.SubElement(root, f"{{{_NS_UA}}}Aliases")
    for name, nid in (
        ("Boolean", "i=1"),
        ("SByte", "i=2"),
        ("Byte", "i=3"),
        ("Int16", "i=4"),
        ("UInt16", "i=5"),
        ("Int32", "i=6"),
        ("UInt32", "i=7"),
        ("Int64", "i=8"),
        ("UInt64", "i=9"),
        ("Float", "i=10"),
        ("Double", "i=11"),
        ("String", "i=12"),
        ("DateTime", "i=13"),
        ("ByteString", "i=15"),
        ("Organizes", "i=35"),
        ("HasTypeDefinition", "i=40"),
        ("HasComponent", "i=47"),
        ("HasProperty", "i=46"),
        ("FolderType", "i=61"),
        ("BaseObjectType", "i=58"),
        ("BaseDataVariableType", "i=63"),
    ):
        alias = ET.SubElement(aliases, f"{{{_NS_UA}}}Alias", {"Alias": name})
        alias.text = nid

    for record in records:
        if record.node_class == "Object":
            node = ET.SubElement(
                root,
                f"{{{_NS_UA}}}UAObject",
                {
                    "NodeId": record.node_id,
                    "BrowseName": f"1:{record.browse_name}",
                    "ParentNodeId": record.parent_node_id,
                },
            )
            dn = ET.SubElement(node, f"{{{_NS_UA}}}DisplayName")
            dn.text = record.display_name
            if record.semantic_id or record.aas_id:
                desc = ET.SubElement(node, f"{{{_NS_UA}}}Description")
                parts = [p for p in (record.model_type, record.aas_id, record.semantic_id) if p]
                desc.text = " | ".join(parts)
            refs = ET.SubElement(node, f"{{{_NS_UA}}}References")
            r1 = ET.SubElement(refs, f"{{{_NS_UA}}}Reference", {"ReferenceType": "HasTypeDefinition"})
            r1.text = "i=58"
            r2 = ET.SubElement(
                refs,
                f"{{{_NS_UA}}}Reference",
                {"ReferenceType": "Organizes", "IsForward": "false"},
            )
            r2.text = record.parent_node_id
        else:
            node = ET.SubElement(
                root,
                f"{{{_NS_UA}}}UAVariable",
                {
                    "NodeId": record.node_id,
                    "BrowseName": f"1:{record.browse_name}",
                    "ParentNodeId": record.parent_node_id,
                    "DataType": record.data_type or "String",
                    "AccessLevel": "3",
                    "UserAccessLevel": "3",
                },
            )
            dn = ET.SubElement(node, f"{{{_NS_UA}}}DisplayName")
            dn.text = record.display_name
            if record.semantic_id:
                desc = ET.SubElement(node, f"{{{_NS_UA}}}Description")
                desc.text = record.semantic_id
            if record.value != "":
                value_el = ET.SubElement(node, f"{{{_NS_UA}}}Value")
                # 단순 문자열 래핑 (서버 측에서 타입 캐스팅)
                uax = ET.SubElement(value_el, "{http://opcfoundation.org/UA/2008/02/Types.xsd}String")
                uax.text = record.value
            refs = ET.SubElement(node, f"{{{_NS_UA}}}References")
            r1 = ET.SubElement(refs, f"{{{_NS_UA}}}Reference", {"ReferenceType": "HasTypeDefinition"})
            r1.text = "i=63"
            r2 = ET.SubElement(
                refs,
                f"{{{_NS_UA}}}Reference",
                {"ReferenceType": "HasComponent", "IsForward": "false"},
            )
            r2.text = record.parent_node_id

    rough = ET.tostring(root, encoding="utf-8")
    parsed = minidom.parseString(rough)
    pretty = parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
    # minidom 이 넣는 중복 XML 선언 정리
    lines = [line for line in pretty.splitlines() if line.strip()]
    if lines and lines[0].startswith("<?xml"):
        return "\n".join(lines) + "\n"
    return '<?xml version="1.0" encoding="utf-8"?>\n' + "\n".join(lines) + "\n"


def _build_engineering_csv(records: list[_NodeRecord]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "NodeId",
            "BrowseName",
            "DisplayName",
            "NodeClass",
            "DataType",
            "ParentNodeId",
            "AasPath",
            "AasId",
            "ModelType",
            "SemanticId",
            "ValueType",
            "Value",
            "AccessLevel",
        ]
    )
    for r in records:
        writer.writerow(
            [
                r.node_id,
                r.browse_name,
                r.display_name,
                r.node_class,
                r.data_type,
                r.parent_node_id,
                r.aas_path,
                r.aas_id,
                r.model_type,
                r.semantic_id,
                r.value_type,
                r.value,
                "CurrentRead | CurrentWrite" if r.node_class == "Variable" else "",
            ]
        )
    return buffer.getvalue()


def _build_syscfg(package: AasxPackage, namespace_uri: str, records: list[_NodeRecord]) -> dict[str, Any]:
    shells = [
        {
            "id": shell.id,
            "idShort": shell.id_short,
            "globalAssetId": shell.asset_information.global_asset_id,
            "assetKind": (
                shell.asset_information.asset_kind.name
                if shell.asset_information.asset_kind
                else None
            ),
            "submodelIds": [str(ref.key[0].value) for ref in shell.submodel],
        }
        for shell in package.shells
    ]
    return {
        "schemaVersion": "1.0.0",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": {
            "packageId": package.id,
            "fileName": package.path.name,
            "aasStandard": "IEC 63278 / IDTA AAS V3",
            "opcUaCompanion": "OPC 30270 (I4AAS)",
        },
        "opcUa": {
            "namespaceUri": namespace_uri,
            "requiredModels": [
                {"modelUri": _NS_UA, "version": "1.04"},
                {"modelUri": _NS_I4AAS, "version": "1.0.0"},
            ],
            "endpoint": {
                "url": "opc.tcp://127.0.0.1:4840",
                "securityMode": "None",
                "securityPolicy": "None",
                "applicationUri": f"urn:camtic:aas:{package.id}",
            },
            "rootNodeId": records[0].node_id if records else "ns=1;i=1000",
            "nodeCount": len(records),
            "variableCount": sum(1 for r in records if r.node_class == "Variable"),
            "objectCount": sum(1 for r in records if r.node_class == "Object"),
        },
        "artifacts": {
            "nodeset": "nodeset.xml",
            "engineering": "engineering.csv",
            "syscfg": "syscfg.json",
        },
        "assetAdministrationShells": shells,
        "counts": {
            "shells": len(package.shells),
            "submodels": len(package.submodels),
            "conceptDescriptions": len(package.concept_descriptions),
            "supplementaryFiles": len(package.files),
        },
    }


def generate_opcua_artifacts(package: AasxPackage, output_dir: Path) -> dict[str, Any]:
    """패키지에서 OPC-UA 산출물을 생성해 ``output_dir`` 에 저장한다."""
    if package.error:
        raise ValueError(f"Cannot generate artifacts for failed package: {package.error}")

    output_dir.mkdir(parents=True, exist_ok=True)
    namespace_uri = _namespace_uri(package)
    records = build_node_records(package)

    nodeset = _build_nodeset_xml(package, records, namespace_uri)
    engineering = _build_engineering_csv(records)
    syscfg = _build_syscfg(package, namespace_uri, records)

    (output_dir / "nodeset.xml").write_text(nodeset, encoding="utf-8")
    (output_dir / "engineering.csv").write_text(engineering, encoding="utf-8-sig")
    (output_dir / "syscfg.json").write_text(
        json.dumps(syscfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    logger.info(
        "Generated OPC-UA artifacts for %s → %s (%d nodes)",
        package.id,
        output_dir,
        len(records),
    )
    return {
        "packageId": package.id,
        "outputDir": str(output_dir),
        "namespaceUri": namespace_uri,
        "artifacts": list(ARTIFACT_NAMES),
        "nodeCount": len(records),
    }


def load_engineering_nodes(output_dir: Path, variables_only: bool = False) -> list[dict[str, Any]]:
    """engineering.csv 를 읽어 클라이언트에서 바로 쓸 노드 목록으로 반환한다."""
    path = output_dir / "engineering.csv"
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    nodes = []
    for row in rows:
        node_class = (row.get("NodeClass") or "").strip()
        if variables_only and node_class != "Variable":
            continue
        nodes.append(
            {
                "nodeId": (row.get("NodeId") or "").strip(),
                "browseName": (row.get("BrowseName") or "").strip(),
                "displayName": (row.get("DisplayName") or "").strip(),
                "nodeClass": node_class,
                "dataType": (row.get("DataType") or "").strip(),
                "parentNodeId": (row.get("ParentNodeId") or "").strip(),
                "aasPath": (row.get("AasPath") or "").strip(),
                "aasId": (row.get("AasId") or "").strip(),
                "modelType": (row.get("ModelType") or "").strip(),
                "semanticId": (row.get("SemanticId") or "").strip(),
                "valueType": (row.get("ValueType") or "").strip(),
                "templateValue": (row.get("Value") or "").strip(),
            }
        )
    return [node for node in nodes if node["nodeId"]]


def list_artifacts(output_dir: Path) -> list[dict[str, Any]]:
    if not output_dir.is_dir():
        return []
    result = []
    for name in ARTIFACT_NAMES:
        path = output_dir / name
        if path.is_file():
            result.append(
                {
                    "name": name,
                    "size": path.stat().st_size,
                    "modifiedAt": datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    ).isoformat(),
                }
            )
    return result
