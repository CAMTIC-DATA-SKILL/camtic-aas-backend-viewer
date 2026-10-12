import binascii
import json
from typing import Any, Optional
from urllib.parse import quote

from basyx.aas import model
from basyx.aas.adapter.json import AASToJsonEncoder, object_store_to_json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .repository import AasxPackage, AasxRepository
from .serializers import TreeBuilder, decode_id, package_summary

router = APIRouter(prefix="/api")


def get_repository(request: Request) -> AasxRepository:
    return request.app.state.repository


def get_package(package_id: str, repository: AasxRepository = Depends(get_repository)) -> AasxPackage:
    package = repository.get(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"Package '{package_id}' not found")
    return package


def get_loaded_package(package: AasxPackage = Depends(get_package)) -> AasxPackage:
    if package.error:
        raise HTTPException(
            status_code=422, detail=f"Package '{package.id}' failed to load: {package.error}"
        )
    return package


def _decode_identifier(encoded: str) -> str:
    try:
        return decode_id(encoded)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Identifier must be base64url encoded")


def _get_identifiable(package: AasxPackage, encoded_id: str, type_: type) -> Any:
    identifier = _decode_identifier(encoded_id)
    obj = package.get_identifiable(identifier, type_)
    if obj is None:
        raise HTTPException(
            status_code=404, detail=f"{type_.__name__} '{identifier}' not found in '{package.id}'"
        )
    return obj


def _aas_json(obj: Any) -> Response:
    return Response(content=json.dumps(obj, cls=AASToJsonEncoder), media_type="application/json")


def _tree_builder(request: Request, package: AasxPackage) -> TreeBuilder:
    root_path = request.scope.get("root_path", "")

    def file_url(name: str) -> str:
        return f"{root_path}/api/packages/{quote(package.id, safe='')}/files/{quote(name.lstrip('/'))}"

    return TreeBuilder(package, file_url)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/packages")
def list_packages(repository: AasxRepository = Depends(get_repository)) -> list[dict[str, Any]]:
    return [package_summary(p) for p in repository.list()]


@router.post("/packages/reload")
def reload_packages(repository: AasxRepository = Depends(get_repository)) -> list[dict[str, Any]]:
    repository.reload()
    return [package_summary(p) for p in repository.list()]


@router.get("/packages/{package_id}")
def get_package_summary(package: AasxPackage = Depends(get_package)) -> dict[str, Any]:
    return package_summary(package)


@router.get("/packages/{package_id}/tree")
def get_package_tree(
    request: Request, package: AasxPackage = Depends(get_loaded_package)
) -> dict[str, Any]:
    return _tree_builder(request, package).package_tree()


@router.get("/packages/{package_id}/environment")
def get_environment(package: AasxPackage = Depends(get_loaded_package)) -> Response:
    """패키지 전체를 표준 AAS JSON Environment 형식으로 반환한다."""
    return Response(content=object_store_to_json(package.objects), media_type="application/json")


@router.get("/packages/{package_id}/shells/{aas_id}")
def get_shell(aas_id: str, package: AasxPackage = Depends(get_loaded_package)) -> Response:
    return _aas_json(_get_identifiable(package, aas_id, model.AssetAdministrationShell))


@router.get("/packages/{package_id}/submodels/{submodel_id}")
def get_submodel(submodel_id: str, package: AasxPackage = Depends(get_loaded_package)) -> Response:
    return _aas_json(_get_identifiable(package, submodel_id, model.Submodel))


@router.get("/packages/{package_id}/submodels/{submodel_id}/tree")
def get_submodel_tree(
    submodel_id: str, request: Request, package: AasxPackage = Depends(get_loaded_package)
) -> dict[str, Any]:
    submodel = _get_identifiable(package, submodel_id, model.Submodel)
    return _tree_builder(request, package).submodel_node(submodel)


@router.get("/packages/{package_id}/concept-descriptions/{cd_id}")
def get_concept_description(
    cd_id: str, package: AasxPackage = Depends(get_loaded_package)
) -> Response:
    return _aas_json(_get_identifiable(package, cd_id, model.ConceptDescription))


@router.get("/packages/{package_id}/thumbnail")
def get_thumbnail(package: AasxPackage = Depends(get_loaded_package)) -> Response:
    if package.thumbnail is None:
        raise HTTPException(status_code=404, detail="Package has no thumbnail")
    return Response(content=package.thumbnail, media_type=_guess_image_type(package.thumbnail))


@router.get("/packages/{package_id}/files/{file_path:path}")
def get_file(file_path: str, package: AasxPackage = Depends(get_loaded_package)) -> Response:
    """AASX 패키지에 포함된 보조 파일(File 요소, 썸네일 등)을 반환한다."""
    name = "/" + file_path
    if name not in package.files:
        raise HTTPException(status_code=404, detail=f"File '{name}' not found in '{package.id}'")
    content, content_type = package.read_file(name)
    file_name = name.rsplit("/", 1)[-1]
    return Response(
        content=content,
        media_type=content_type or "application/octet-stream",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(file_name)}"},
    )


def _guess_image_type(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[8:12] == b"WEBP":
        return "image/webp"
    if data.lstrip().startswith(b"<svg") or b"<svg" in data[:256]:
        return "image/svg+xml"
    return "application/octet-stream"
