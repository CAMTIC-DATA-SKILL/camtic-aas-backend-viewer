import io
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TypeVar

import pyecma376_2
from basyx.aas import model
from basyx.aas.adapter import aasx

from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)

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
    """지정된 폴더의 모든 ``*.aasx`` 파일을 메모리에 로드하여 보관한다."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._packages: dict[str, AasxPackage] = {}
        self._lock = threading.Lock()

    def reload(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        paths = sorted(
            p for p in self.data_dir.iterdir() if p.is_file() and p.suffix.lower() == ".aasx"
        )
        if not paths:
            logger.warning("No .aasx files found in %s", self.data_dir)

        packages: dict[str, AasxPackage] = {}
        for path in paths:
            package_id = path.stem
            suffix = 2
            while package_id in packages:
                package_id = f"{path.stem}-{suffix}"
                suffix += 1
            packages[package_id] = load_package(path, package_id)

        with self._lock:
            self._packages = packages

    def list(self) -> list[AasxPackage]:
        with self._lock:
            return list(self._packages.values())

    def get(self, package_id: str) -> Optional[AasxPackage]:
        with self._lock:
            return self._packages.get(package_id)
