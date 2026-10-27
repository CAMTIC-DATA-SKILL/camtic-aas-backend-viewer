import binascii
import io
import json
import zipfile
from typing import Any, Optional
from urllib.parse import quote

from basyx.aas import model
from basyx.aas.adapter.json import AASToJsonEncoder, object_store_to_json
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from .opcua_export import ARTIFACT_NAMES, generate_opcua_artifacts, list_artifacts
from .repository import AasxPackage, AasxRepository
from .serializers import TreeBuilder, decode_id, encode_id, package_summary

router = APIRouter(prefix="/api")

_ARTIFACT_MEDIA = {
    "nodeset.xml": "application/xml",
    "engineering.csv": "text/csv",
    "syscfg.json": "application/json",
}


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


def _resolve_shell_id(aas_id: str) -> str:
    """base64url 인코딩된 AAS id 또는 원문 URN/IRI 를 허용한다."""
    try:
        return decode_id(aas_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        if ":" in aas_id or "/" in aas_id:
            return aas_id
        raise HTTPException(
            status_code=400,
            detail="AAS id must be base64url encoded, or a raw URN/IRI",
        )


def _package_by_shell_id(aas_id: str, repository: AasxRepository) -> tuple[str, AasxPackage]:
    shell_id = _resolve_shell_id(aas_id)
    package = repository.find_by_shell_id(shell_id)
    if package is None:
        raise HTTPException(
            status_code=404,
            detail=f"No package found for AssetAdministrationShell id '{shell_id}'",
        )
    return shell_id, package


def _artifacts_payload(package: AasxPackage, repository: AasxRepository, shell_id: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "packageId": package.id,
        "artifacts": list_artifacts(repository.artifact_dir(package.id)),
    }
    if shell_id is not None:
        payload["aasId"] = shell_id
        payload["aasIdBase64"] = encode_id(shell_id)
    return payload


def _artifacts_zip_response(package: AasxPackage, repository: AasxRepository) -> StreamingResponse:
    artifact_dir = repository.artifact_dir(package.id)
    files = [artifact_dir / name for name in ARTIFACT_NAMES if (artifact_dir / name).is_file()]
    if not files:
        raise HTTPException(
            status_code=404, detail="No OPC-UA artifacts found. Upload or regenerate first."
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=path.name)
    buffer.seek(0)
    filename = f"{package.id}_opcua_artifacts.zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _artifact_file_response(
    package: AasxPackage, repository: AasxRepository, artifact_name: str
) -> FileResponse:
    if artifact_name not in ARTIFACT_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown artifact '{artifact_name}'. Allowed: {', '.join(ARTIFACT_NAMES)}",
        )
    path = repository.artifact_dir(package.id) / artifact_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_name}' not found")
    return FileResponse(
        path=path,
        media_type=_ARTIFACT_MEDIA.get(artifact_name, "application/octet-stream"),
        filename=artifact_name,
    )


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


def _summary(package: AasxPackage, repository: AasxRepository) -> dict[str, Any]:
    artifacts = list_artifacts(repository.artifact_dir(package.id))
    return package_summary(package, artifact_count=len(artifacts))


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/packages")
def list_packages(repository: AasxRepository = Depends(get_repository)) -> list[dict[str, Any]]:
    return [_summary(p, repository) for p in repository.list()]


@router.post("/packages/reload")
def reload_packages(repository: AasxRepository = Depends(get_repository)) -> list[dict[str, Any]]:
    repository.reload()
    for package in repository.list():
        if package.error or list_artifacts(repository.artifact_dir(package.id)):
            continue
        try:
            generate_opcua_artifacts(package, repository.artifact_dir(package.id))
        except Exception:
            pass
    return [_summary(p, repository) for p in repository.list()]


@router.post("/packages")
async def upload_package(
    file: UploadFile = File(...),
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    """AASX 파일을 업로드하고 OPC-UA 산출물(nodeset.xml, engineering.csv, syscfg.json)을 생성한다."""
    filename = file.filename or ""
    if not filename.lower().endswith(".aasx"):
        raise HTTPException(status_code=400, detail="Only .aasx files are allowed")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        package = repository.save_upload(filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    artifacts_info: Optional[dict[str, Any]] = None
    if not package.error:
        try:
            artifacts_info = generate_opcua_artifacts(
                package, repository.artifact_dir(package.id)
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Package saved but artifact generation failed: {e}"
            ) from e

    return {
        "package": _summary(package, repository),
        "artifacts": artifacts_info,
    }


@router.get("/packages/{package_id}")
def get_package_summary(
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    return _summary(package, repository)


@router.delete("/packages/{package_id}")
def delete_package(
    package_id: str,
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    """AASX 패키지와 관련 OPC-UA 산출물을 삭제한다."""
    if repository.get(package_id) is None:
        raise HTTPException(status_code=404, detail=f"Package '{package_id}' not found")
    if not repository.delete(package_id):
        raise HTTPException(status_code=404, detail=f"Package '{package_id}' not found")
    return {"deleted": package_id}


@router.get("/packages/{package_id}/download")
def download_package(package: AasxPackage = Depends(get_package)) -> FileResponse:
    """원본 AASX 패키지 파일을 다운로드한다."""
    if not package.path.is_file():
        raise HTTPException(status_code=404, detail="AASX file not found on disk")
    return FileResponse(
        path=package.path,
        media_type="application/asset-administration-shell-package",
        filename=package.path.name,
    )


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


@router.get("/packages/{package_id}/artifacts")
def get_artifacts(
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    return _artifacts_payload(package, repository)


@router.post("/packages/{package_id}/artifacts/generate")
def regenerate_artifacts(
    package: AasxPackage = Depends(get_loaded_package),
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    try:
        info = generate_opcua_artifacts(package, repository.artifact_dir(package.id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return info


@router.get("/packages/{package_id}/artifacts/download")
def download_artifacts_zip(
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
) -> StreamingResponse:
    """OPC-UA 산출물(nodeset.xml, engineering.csv, syscfg.json)을 ZIP으로 다운로드한다."""
    return _artifacts_zip_response(package, repository)


@router.get("/packages/{package_id}/artifacts/{artifact_name}")
def download_artifact(
    artifact_name: str,
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
) -> FileResponse:
    return _artifact_file_response(package, repository, artifact_name)


# ---------------------------------------------------------------- shells (by AAS id)
# aas_id: AAS Part 2 와 동일하게 base64url 인코딩 권장. 슬래시가 없는 URN 원문도 허용.


@router.get("/shells/{aas_id}/artifacts")
def get_shell_artifacts(
    aas_id: str,
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    """AAS id 로 OPC-UA 산출물 목록을 반환한다."""
    shell_id, package = _package_by_shell_id(aas_id, repository)
    return _artifacts_payload(package, repository, shell_id)


@router.post("/shells/{aas_id}/artifacts/generate")
def regenerate_shell_artifacts(
    aas_id: str,
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    shell_id, package = _package_by_shell_id(aas_id, repository)
    if package.error:
        raise HTTPException(
            status_code=422, detail=f"Package '{package.id}' failed to load: {package.error}"
        )
    try:
        info = generate_opcua_artifacts(package, repository.artifact_dir(package.id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    info["aasId"] = shell_id
    info["aasIdBase64"] = encode_id(shell_id)
    return info


@router.get("/shells/{aas_id}/artifacts/download")
def download_shell_artifacts_zip(
    aas_id: str,
    repository: AasxRepository = Depends(get_repository),
) -> StreamingResponse:
    """AAS id 로 nodeset.xml / engineering.csv / syscfg.json ZIP 을 다운로드한다."""
    _, package = _package_by_shell_id(aas_id, repository)
    return _artifacts_zip_response(package, repository)


@router.get("/shells/{aas_id}/artifacts/{artifact_name}")
def download_shell_artifact(
    aas_id: str,
    artifact_name: str,
    repository: AasxRepository = Depends(get_repository),
) -> FileResponse:
    """AAS id 로 개별 OPC-UA 산출물을 다운로드한다."""
    _, package = _package_by_shell_id(aas_id, repository)
    return _artifact_file_response(package, repository, artifact_name)


@router.get("/shells/{aas_id}")
def get_shell_by_id(
    aas_id: str,
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    """AssetAdministrationShell.id 로 패키지/셸 요약을 반환한다."""
    shell_id, package = _package_by_shell_id(aas_id, repository)
    shell = next(s for s in package.shells if s.id == shell_id)
    return {
        "aasId": shell_id,
        "aasIdBase64": encode_id(shell_id),
        "idShort": shell.id_short,
        "globalAssetId": shell.asset_information.global_asset_id,
        "package": _summary(package, repository),
    }


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
