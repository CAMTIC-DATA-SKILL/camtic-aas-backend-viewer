import io
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import pyecma376_2
from basyx.aas import model
from basyx.aas.adapter import aasx

from config import StorageLayout
from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)

_SAFE_FILENAME = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)

_CORE_PROPERTY_NAMES = (
    "title",
    "subject",
    "creator",
    "description",
    "keywords",
    "version",
    "revision",
    "created",
    "modified",
    "lastModifiedBy",
)

_I = TypeVar("_I", bound=model.Identifiable)


@dataclass
class AasxPackage:
    id: str
    path: Path
    loaded_at: datetime
    objects: model.DictIdentifiableStore = field(default_factory=model.DictIdentifiableStore)
    files: aasx.DictSupplementaryFileContainer = field(
        default_factory=aasx.DictSupplementaryFileContainer
    )
    core_properties: dict[str, str] = field(default_factory=dict)
    thumbnail: Optional[bytes] = None
    error: Optional[str] = None

    @property
    def shells(self) -> list[model.AssetAdministrationShell]:
        return [o for o in self.objects if isinstance(o, model.AssetAdministrationShell)]

    @property
    def submodels(self) -> list[model.Submodel]:
        return [o for o in self.objects if isinstance(o, model.Submodel)]

    @property
    def concept_descriptions(self) -> list[model.ConceptDescription]:
        return [o for o in self.objects if isinstance(o, model.ConceptDescription)]

    @property
    def file_name(self) -> str:
        """디스크 파일은 package.aasx 로 고정하고, 화면과 다운로드에는 패키지 id 를 쓴다."""
        return f"{self.id}.aasx"

    def get_identifiable(self, identifier: str, type_: type[_I]) -> Optional[_I]:
        obj = self.objects.get(identifier)
        return obj if isinstance(obj, type_) else None

    def read_file(self, name: str) -> tuple[bytes, str]:
        buffer = io.BytesIO()
        self.files.write_file(name, buffer)
        return buffer.getvalue(), self.files.get_content_type(name)


def _core_properties_to_dict(props: pyecma376_2.OPCCoreProperties) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in _CORE_PROPERTY_NAMES:
        value = getattr(props, name, None)
        if value is None or value == "":
            continue
        result[name] = value.isoformat() if isinstance(value, datetime) else str(value)
    return result


def load_package(path: Path, package_id: str) -> AasxPackage:
    package = AasxPackage(id=package_id, path=path, loaded_at=datetime.now(timezone.utc))
    try:
        with aasx.AASXReader(path) as reader:
            reader.read_into(object_store=package.objects, file_store=package.files)
            try:
                package.core_properties = _core_properties_to_dict(reader.get_core_properties())
                package.thumbnail = reader.get_thumbnail()
            except Exception:
                logger.warning("Failed to read metadata of %s", path, exc_info=True)
    except Exception as e:
        logger.exception("Failed to load AASX package %s", path)
        package.error = f"{type(e).__name__}: {e}"
    else:
        logger.info(
            "Loaded %s: %d shells, %d submodels, %d concept descriptions, %d files",
            path.name,
            len(package.shells),
            len(package.submodels),
            len(package.concept_descriptions),
            len(package.files),
        )
    return package


class AasxRepository:
    """``packages/{package_id}/package.aasx`` 를 메모리에 로드하여 보관한다."""

    def __init__(self, layout: StorageLayout):
        self.layout = layout
        self._packages: dict[str, AasxPackage] = {}
        self._lock = threading.Lock()

    def package_dir(self, package_id: str) -> Path:
        return self.layout.package_dir(package_id)

    def artifact_dir(self, package_id: str) -> Path:
        return self.layout.opcua_dir(package_id)

    def digital_twin_dir(self, kind: str, package_id: str) -> Path:
        if kind == "step":
            return self.layout.cad_dir(package_id)
        if kind == "usd":
            return self.layout.usd_dir(package_id)
        raise ValueError(f"디지털 트윈 저장 종류가 아닙니다: {kind}")

    def write_digital_twin_file(self, package_id: str, kind: str, filename: str, content: bytes) -> Path:
        dest_dir = self.digital_twin_dir(kind, package_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for old in dest_dir.iterdir():
            if old.is_file():
                old.unlink()
        dest = dest_dir / _safe_disk_name(filename)
        dest.write_bytes(content)
        logger.info("Saved digital twin %s file %s", kind, dest)
        return dest

    def find_digital_twin_file(self, package_id: str, kind: str) -> Optional[Path]:
        dest_dir = self.digital_twin_dir(kind, package_id)
        if not dest_dir.is_dir():
            return None
        suffixes = {".stp", ".step"} if kind == "step" else {".usd", ".usda", ".usdc", ".usdz"}
        matches = sorted(
            path for path in dest_dir.iterdir() if path.is_file() and path.suffix.lower() in suffixes
        )
        return matches[0] if matches else None

    def find_mesh_file(self, package_id: str) -> Optional[Path]:
        dest_dir = self.digital_twin_dir("step", package_id)
        if not dest_dir.is_dir():
            return None
        matches = sorted(
            path for path in dest_dir.iterdir() if path.is_file() and path.suffix.lower() == ".glb"
        )
        return matches[0] if matches else None

    def reload(self) -> None:
        self.layout.ensure_tree()
        packages: dict[str, AasxPackage] = {}
        entries = sorted(
            (path for path in self.layout.packages_dir.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
        for entry in entries:
            aasx_path = entry / self.layout.package_aasx_name
            if not aasx_path.is_file():
                continue
            packages[entry.name] = load_package(aasx_path, entry.name)
        if not packages:
            logger.warning("No AASX packages found in %s", self.layout.packages_dir)

        with self._lock:
            self._packages = packages

    def list(self) -> list[AasxPackage]:
        with self._lock:
            return list(self._packages.values())

    def get(self, package_id: str) -> Optional[AasxPackage]:
        with self._lock:
            return self._packages.get(package_id)

    def find_by_shell_id(self, shell_id: str) -> Optional[AasxPackage]:
        """AssetAdministrationShell.id 로 패키지를 찾는다."""
        with self._lock:
            for package in self._packages.values():
                if package.error:
                    continue
                if any(shell.id == shell_id for shell in package.shells):
                    return package
            return None

    def save_upload(self, filename: str, content: bytes) -> AasxPackage:
        """AASX 바이트를 ``packages/{id}/package.aasx`` 에 저장하고 로드한다. 같은 id 는 덮어쓴다."""
        safe_name = _sanitize_aasx_filename(filename)
        package_id = safe_name[:-5]
        target = self.layout.aasx_path(package_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        logger.info("Saved uploaded AASX to %s (%d bytes)", target, len(content))

        package = load_package(target, package_id)
        with self._lock:
            self._packages[package_id] = package
            return package

    def rewrite(self, package: AasxPackage) -> AasxPackage:
        """메모리의 AAS 객체·첨부 파일을 AASX로 다시 쓰고 패키지를 재로드한다."""
        with self._lock:
            return self._rewrite_locked(package)

    def update_and_rewrite(
        self, package_id: str, mutator: Callable[[AasxPackage], Any]
    ) -> tuple[AasxPackage, Any]:
        """잠금 안에서 패키지를 변경한 뒤 AASX를 저장하고 재로드한다."""
        with self._lock:
            package = self._packages.get(package_id)
            if package is None:
                raise KeyError(package_id)
            extra = mutator(package)
            return self._rewrite_locked(package), extra

    def _rewrite_locked(self, package: AasxPackage) -> AasxPackage:
        current = self._packages.get(package.id)
        if current is None:
            raise KeyError(package.id)
        write_package(package)
        reloaded = load_package(package.path, package.id)
        self._packages[package.id] = reloaded
        return reloaded

    def delete(self, package_id: str) -> bool:
        """패키지 폴더(AASX, 산출물, CAD/USD, 패키지 설정)를 삭제하고 메모리에서 제거한다."""
        with self._lock:
            package = self._packages.get(package_id)
            if package is None:
                return False
            del self._packages[package_id]

        package_dir = self.layout.package_dir(package_id)
        if package_dir.is_dir():
            shutil.rmtree(package_dir)
            logger.info("Deleted package directory %s", package_dir)
        return True


def write_aasx_bytes(
    package: AasxPackage, objects: Optional[model.DictIdentifiableStore] = None
) -> bytes:
    """패키지 첨부 파일을 유지한 채 object store 를 AASX 바이트로 만든다. 디스크 원본은 건드리지 않는다."""
    buffer = io.BytesIO()
    with aasx.AASXWriter(buffer, failsafe=False) as writer:
        writer.write_all_aas_objects(
            "/aasx/data.json",
            package.objects if objects is None else objects,
            package.files,
            write_json=True,
        )
        _write_thumbnail(writer, package)
        _write_core_properties(writer, package)
    return buffer.getvalue()


def write_package(package: AasxPackage) -> None:
    """현재 메모리 상태를 원본 AASX 경로에 원자적으로 덮어쓴다."""
    target = package.path
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_bytes(write_aasx_bytes(package))
        tmp.replace(target)
        logger.info("Rewrote AASX package %s", target)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _write_thumbnail(writer: aasx.AASXWriter, package: AasxPackage) -> None:
    if not package.thumbnail:
        return
    media = _guess_image_type(package.thumbnail)
    ext = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }.get(media, ".bin")
    writer.write_thumbnail(f"/thumbnail{ext}", bytearray(package.thumbnail), media)


def _write_core_properties(writer: aasx.AASXWriter, package: AasxPackage) -> None:
    if not package.core_properties:
        return
    props = pyecma376_2.OPCCoreProperties()
    for name, value in package.core_properties.items():
        if not hasattr(props, name) or value in (None, ""):
            continue
        if name in {"created", "modified", "lastPrinted"} and isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                continue
        setattr(props, name, value)
    writer.write_core_properties(props)


def _guess_image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.lstrip().startswith(b"<svg") or b"<svg" in data[:256]:
        return "image/svg+xml"
    return "application/octet-stream"


def _safe_disk_name(filename: str) -> str:
    name = Path(filename).name
    cleaned = _SAFE_FILENAME.sub("_", name).strip(" ._")
    return cleaned or "file.bin"


def _sanitize_aasx_filename(filename: str) -> str:
    name = Path(filename).name
    if not name.lower().endswith(".aasx"):
        raise ValueError("Only .aasx files are allowed")
    stem = name[:-5]
    cleaned = _SAFE_FILENAME.sub("_", stem).strip(" ._")
    if not cleaned:
        raise ValueError("Invalid AASX file name")
    return f"{cleaned}.aasx"
