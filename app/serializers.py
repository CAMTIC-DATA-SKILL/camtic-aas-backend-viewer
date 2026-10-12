"""BaSyx 모델 객체를 프론트엔드 렌더링용 간소화 JSON 트리로 변환한다.

표준 AAS JSON 직렬화는 ``basyx.aas.adapter.json`` 을 그대로 사용하고,
이 모듈은 화면 표시에 필요한 값만 추린 트리 구조를 만든다.
"""

import base64
from enum import Enum
from typing import Any, Callable, Iterable, Optional

from basyx.aas import model

from .repository import AasxPackage

FileUrlResolver = Callable[[str], str]


def encode_id(identifier: str) -> str:
    return base64.urlsafe_b64encode(identifier.encode("utf-8")).decode("ascii").rstrip("=")


def decode_id(encoded: str) -> str:
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None and v != [] and v != {}}


def _enum_name(value: Optional[Enum]) -> Optional[str]:
    if value is None:
        return None
    return "".join(part.capitalize() for part in value.name.split("_"))


def _lang(value: Optional[model.LangStringSet]) -> Optional[dict[str, str]]:
    return dict(value) if value else None


def _reference(ref: Optional[model.Reference]) -> Optional[str]:
    if ref is None or not ref.key:
        return None
    return " / ".join(str(key.value) for key in ref.key)


def _xsd(value: Any) -> Optional[str]:
    return None if value is None else model.datatypes.xsd_repr(value)


def _value_type(type_: Optional[type]) -> Optional[str]:
    return model.datatypes.XSD_TYPE_NAMES.get(type_) if type_ else None


def _administration(admin: Optional[model.AdministrativeInformation]) -> Optional[dict]:
    if admin is None:
        return None
    return _compact(
        {
            "version": admin.version,
            "revision": admin.revision,
            "creator": _reference(admin.creator),
            "templateId": admin.template_id,
        }
    ) or None


def _specific_asset_ids(ids: Iterable[model.SpecificAssetId]) -> list[dict[str, str]]:
    return [{"name": s.name, "value": s.value} for s in ids]


def package_summary(package: AasxPackage) -> dict[str, Any]:
    return _compact(
        {
            "id": package.id,
            "fileName": package.path.name,
            "loadedAt": package.loaded_at.isoformat(),
            "error": package.error,
            "coreProperties": package.core_properties,
            "hasThumbnail": package.thumbnail is not None,
            "counts": {
                "shells": len(package.shells),
                "submodels": len(package.submodels),
                "conceptDescriptions": len(package.concept_descriptions),
                "files": len(package.files),
            },
            "shells": [
                _compact({"id": s.id, "idBase64": encode_id(s.id), "idShort": s.id_short})
                for s in package.shells
            ],
        }
    )


class TreeBuilder:
    def __init__(self, package: AasxPackage, file_url: FileUrlResolver):
        self.package = package
        self._file_url = file_url
        self.concepts = {cd.id: self._concept(cd) for cd in package.concept_descriptions}

    def package_tree(self) -> dict[str, Any]:
        referenced: set[str] = set()
        shells = [self.shell_node(shell, referenced) for shell in self.package.shells]
        orphans = [
            self.submodel_node(sm) for sm in self.package.submodels if sm.id not in referenced
        ]
        return {
            "package": package_summary(self.package),
            "shells": shells,
            "orphanSubmodels": orphans,
            "conceptDescriptions": self.concepts,
        }

    def shell_node(
        self, shell: model.AssetAdministrationShell, referenced: Optional[set[str]] = None
    ) -> dict[str, Any]:
        submodels = []
        for ref in sorted(shell.submodel, key=lambda r: str(r.key[0].value)):
            submodel_id = str(ref.key[0].value)
            if referenced is not None:
                referenced.add(submodel_id)
            submodel = self.package.get_identifiable(submodel_id, model.Submodel)
            if submodel is None:
                submodels.append(
                    {
                        "modelType": "Submodel",
                        "id": submodel_id,
                        "idBase64": encode_id(submodel_id),
                        "missing": True,
                    }
                )
            else:
                submodels.append(self.submodel_node(submodel))

        info = shell.asset_information
        return _compact(
            {
                "modelType": "AssetAdministrationShell",
                "id": shell.id,
                "idBase64": encode_id(shell.id),
                **self._referable(shell),
                "administration": _administration(shell.administration),
                "derivedFrom": _reference(shell.derived_from),
                "assetInformation": _compact(
                    {
                        "assetKind": _enum_name(info.asset_kind),
                        "globalAssetId": info.global_asset_id,
                        "assetType": info.asset_type,
                        "specificAssetIds": _specific_asset_ids(info.specific_asset_id),
                        "thumbnail": self._resource(info.default_thumbnail),
                    }
                ),
                "submodels": submodels,
            }
        )

    def submodel_node(self, submodel: model.Submodel) -> dict[str, Any]:
        return _compact(
            {
                "modelType": "Submodel",
                "id": submodel.id,
                "idBase64": encode_id(submodel.id),
                **self._referable(submodel),
                **self._semantics(submodel),
                "kind": _enum_name(submodel.kind),
                "administration": _administration(submodel.administration),
                "children": [
                    self.element_node(e, str(e.id_short)) for e in submodel.submodel_element
                ],
            }
        )

    def element_node(self, element: model.SubmodelElement, path: str) -> dict[str, Any]:
        node: dict[str, Any] = {
            "modelType": type(element).__name__,
            "path": path,
            **self._referable(element),
            **self._semantics(element),
            "qualifiers": [
                _compact(
                    {
                        "type": q.type,
                        "valueType": _value_type(q.value_type),
                        "value": _xsd(q.value),
                        "kind": _enum_name(q.kind),
                    }
                )
                for q in element.qualifier
            ],
        }

        if isinstance(element, model.Property):
            node.update(
                valueType=_value_type(element.value_type),
                value=_xsd(element.value),
                valueId=_reference(element.value_id),
            )
        elif isinstance(element, model.MultiLanguageProperty):
            node.update(value=_lang(element.value), valueId=_reference(element.value_id))
        elif isinstance(element, model.Range):
            node.update(
                valueType=_value_type(element.value_type),
                min=_xsd(element.min),
                max=_xsd(element.max),
            )
        elif isinstance(element, model.Blob):
            node.update(
                contentType=element.content_type,
                size=len(element.value) if element.value else 0,
            )
        elif isinstance(element, model.File):
            node.update(
                contentType=element.content_type,
                value=element.value,
                url=self._file_url_for(element.value),
            )
        elif isinstance(element, model.ReferenceElement):
            node.update(value=_reference(element.value))
        elif isinstance(element, model.SubmodelElementCollection):
            node["children"] = [self._named_child(c, path) for c in element.value]
        elif isinstance(element, model.SubmodelElementList):
            node.update(
                typeValueListElement=element.type_value_list_element.__name__,
                valueTypeListElement=_value_type(element.value_type_list_element),
                orderRelevant=element.order_relevant,
                children=[
                    self.element_node(c, f"{path}[{i}]") for i, c in enumerate(element.value)
                ],
            )
        elif isinstance(element, model.RelationshipElement):
            node.update(first=_reference(element.first), second=_reference(element.second))
            if isinstance(element, model.AnnotatedRelationshipElement):
                node["children"] = [self._named_child(c, path) for c in element.annotation]
        elif isinstance(element, model.Operation):
            children = []
            for role, variables in (
                ("input", element.input_variable),
                ("output", element.output_variable),
                ("inoutput", element.in_output_variable),
            ):
                for variable in variables:
                    children.append({**self._named_child(variable, path), "variableRole": role})
            node["children"] = children
        elif isinstance(element, model.Entity):
            node.update(
                entityType=_enum_name(element.entity_type),
                globalAssetId=element.global_asset_id,
                specificAssetIds=_specific_asset_ids(element.specific_asset_id),
                children=[self._named_child(c, path) for c in element.statement],
            )
        elif isinstance(element, model.BasicEventElement):
            node.update(
                observed=_reference(element.observed),
                direction=_enum_name(element.direction),
                state=_enum_name(element.state),
                messageTopic=element.message_topic,
            )

        return _compact(node)

    def _named_child(self, child: model.SubmodelElement, parent_path: str) -> dict[str, Any]:
        return self.element_node(child, f"{parent_path}.{child.id_short}")

    @staticmethod
    def _referable(obj: model.Referable) -> dict[str, Any]:
        # SubmodelElementList 의 자식은 SDK가 내부적으로 임의의 id_short 를 부여하므로 노출하지 않는다.
        in_list = isinstance(obj.parent, model.SubmodelElementList)
        return {
            "idShort": None if in_list else obj.id_short,
            "displayName": _lang(obj.display_name),
            "description": _lang(obj.description),
            "category": obj.category,
        }

    def _semantics(self, obj: model.HasSemantics) -> dict[str, Any]:
        semantic_id = obj.semantic_id
        concept_id = None
        if semantic_id is not None:
            concept_id = next(
                (str(k.value) for k in semantic_id.key if str(k.value) in self.concepts), None
            )
        return {"semanticId": _reference(semantic_id), "conceptId": concept_id}

    def _resource(self, resource: Optional[model.Resource]) -> Optional[dict[str, Any]]:
        if resource is None:
            return None
        return _compact(
            {
                "path": resource.path,
                "contentType": resource.content_type,
                "url": self._file_url_for(resource.path),
            }
        )

    def _file_url_for(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        if path in self.package.files:
            return self._file_url(path)
        if path.startswith(("http://", "https://")):
            return path
        return None

    @staticmethod
    def _concept(cd: model.ConceptDescription) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "id": cd.id,
            "idBase64": encode_id(cd.id),
            **TreeBuilder._referable(cd),
            "isCaseOf": [_reference(r) for r in cd.is_case_of],
        }
        for eds in cd.embedded_data_specifications:
            content = eds.data_specification_content
            if isinstance(content, model.DataSpecificationIEC61360):
                summary.update(
                    preferredName=_lang(content.preferred_name),
                    shortName=_lang(content.short_name),
                    definition=_lang(content.definition),
                    unit=content.unit,
                    symbol=content.symbol,
                    dataType=content.data_type.name if content.data_type else None,
                )
                break
        return _compact(summary)
