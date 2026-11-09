from pathlib import Path

import pytest
from basyx.aas import model
from basyx.aas.adapter import aasx

from app.documentation import (
    Attachment,
    apply_attachments,
    documentation_needs_patch,
    patch_documentation_slots,
    serialize_documentation,
)
from app.repository import AasxRepository
from config import resolve_storage
from scripts.create_sample_aasx import build_camtic_sample, write_aasx


def _repository(tmp_path: Path) -> AasxRepository:
    repo = AasxRepository(resolve_storage(tmp_path))
    repo.reload()
    return repo


def _write_package(tmp_path: Path, package_id: str, *args, **kwargs) -> None:
    path = resolve_storage(tmp_path).aasx_path(package_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_aasx(path, *args, **kwargs)


def _repo_with_sample(tmp_path: Path) -> tuple[AasxRepository, str]:
    _write_package(tmp_path, "camtic_sample", *build_camtic_sample())
    return _repository(tmp_path), "camtic_sample"


def test_attach_creates_documentation_submodel(tmp_path: Path):
    repo, package_id = _repo_with_sample(tmp_path)
    package = repo.get(package_id)
    assert package is not None
    assert serialize_documentation(package, lambda name: name)["submodels"] == []

    saved, added = repo.update_and_rewrite(
        package_id,
        lambda pkg: apply_attachments(
            pkg,
            [
                Attachment("step", "cell.stp", b"ISO-10303-STEP", "application/step"),
                Attachment("usd", "cell.usda", b"#usda 1.0\n", "model/vnd.usda+ascii"),
                Attachment("file", "mesh.glb", b"glTF", "model/gltf-binary"),
            ],
        ),
    )
    assert added["created"] is True
    docs = serialize_documentation(saved, lambda name: f"/files{name}")
    assert len(docs["submodels"]) == 1
    files = {item["idShort"]: item for item in docs["submodels"][0]["files"]}
    assert [item["idShort"] for item in docs["submodels"][0]["files"][:2]] == [
        "DigitalTwinCadModel",
        "DigitalTwinUsdModel",
    ]
    assert files["DigitalTwinCadModel"]["kind"] == "step"
    assert files["DigitalTwinCadModel"]["fileName"] == "cell.stp"
    assert files["DigitalTwinCadModel"]["semanticId"] == "urn:physical-ai:ids/cd/DigitalTwinCadModel/1/0"
    assert files["DigitalTwinCadModel"]["reserved"] is True
    assert files["DigitalTwinCadModel"]["missing"] is False
    assert files["DigitalTwinUsdModel"]["kind"] == "usd"
    assert files["DigitalTwinUsdModel"]["semanticId"] == "urn:physical-ai:ids/cd/DigitalTwinUsdModel/1/0"
    assert files["DigitalTwinUsdModel"]["reserved"] is True
    assert files["mesh"]["kind"] == "file"
    assert files["mesh"]["reserved"] is False
    assert files["DigitalTwinCadModel"]["url"] == "/files/aasx/suppl/cell.stp"

    reloaded = repo.get(package_id)
    assert reloaded is not None
    assert any(sm.id_short == "Documentation" for sm in reloaded.submodels)
    content, content_type = reloaded.read_file("/aasx/suppl/cell.stp")
    assert content == b"ISO-10303-STEP"
    assert content_type == "application/step"


def test_attach_replaces_existing_step_file(tmp_path: Path):
    repo, package_id = _repo_with_sample(tmp_path)
    repo.update_and_rewrite(
        package_id,
        lambda pkg: apply_attachments(
            pkg, [Attachment("step", "old.stp", b"old-step", id_short="STEP")]
        ),
    )
    saved, added = repo.update_and_rewrite(
        package_id,
        lambda pkg: apply_attachments(
            pkg, [Attachment("step", "new.stp", b"new-step", id_short="STEP")]
        ),
    )
    assert added["created"] is False
    assert added["added"][0]["replaced"] is True
    docs = serialize_documentation(saved, lambda name: name)
    step = next(item for item in docs["submodels"][0]["files"] if item["kind"] == "step")
    assert step["fileName"] == "new.stp"
    content, _ = saved.read_file(step["packagePath"])
    assert content == b"new-step"


def test_existing_documentation_submodel_is_reused(tmp_path: Path):
    files = aasx.DictSupplementaryFileContainer()
    documentation = model.Submodel(
        id_="https://example.org/sm/Documentation",
        id_short="Documentation",
        submodel_element=[model.File("Readme", content_type="text/plain", value="/aasx/suppl/readme.txt")],
    )
    files.add_file("/aasx/suppl/readme.txt", __import__("io").BytesIO(b"hello"), "text/plain")
    shell = model.AssetAdministrationShell(
        id_="https://example.org/aas/demo",
        id_short="Demo",
            asset_information=model.AssetInformation(
                asset_kind=model.AssetKind.INSTANCE,
                global_asset_id="https://example.org/asset/demo",
            ),
        submodel={model.ModelReference.from_referable(documentation)},
    )
    store = model.DictIdentifiableStore([shell, documentation])
    _write_package(tmp_path, "demo", store, files)

    repo = _repository(tmp_path)
    saved, added = repo.update_and_rewrite(
        "demo",
        lambda pkg: apply_attachments(pkg, [Attachment("usd", "scene.usdz", b"PK\x03\x04")]),
    )
    assert added["created"] is False
    docs = serialize_documentation(saved, lambda name: name)
    files = {item["idShort"]: item for item in docs["submodels"][0]["files"]}
    assert files["DigitalTwinCadModel"]["missing"] is True
    assert files["DigitalTwinUsdModel"]["fileName"] == "scene.usdz"
    assert files["DigitalTwinUsdModel"]["semanticId"] == "urn:physical-ai:ids/cd/DigitalTwinUsdModel/1/0"
    assert files["Readme"]["kind"] == "file"
    assert {item["kind"] for item in files.values()} == {"file", "usd", "step"}


def test_custom_file_name_and_reject_non_step(tmp_path: Path):
    repo, package_id = _repo_with_sample(tmp_path)
    saved, added = repo.update_and_rewrite(
        package_id,
        lambda pkg: apply_attachments(
            pkg,
            [Attachment("step", "cell.stp", b"ISO-10303-STEP", id_short="RobotCAD")],
        ),
    )
    assert added["added"][0]["idShort"] == "RobotCAD"
    files = {item["idShort"]: item for item in serialize_documentation(saved, lambda n: n)["submodels"][0]["files"]}
    assert files["RobotCAD"]["kind"] == "step"
    assert files["DigitalTwinCadModel"]["missing"] is True
    assert files["DigitalTwinUsdModel"]["missing"] is True

    package = repo.get(package_id)
    with pytest.raises(ValueError, match="STEP 규격"):
        apply_attachments(package, [Attachment("step", "notes.pdf", b"%PDF", id_short="Manual")])
    with pytest.raises(ValueError, match="메인 디지털 트윈 CAD"):
        apply_attachments(package, [Attachment("file", "notes.txt", b"note", id_short="STEP")])


def test_patch_creates_empty_step_and_usd_slots(tmp_path: Path):
    repo, package_id = _repo_with_sample(tmp_path)
    package = repo.get(package_id)
    assert package is not None
    assert documentation_needs_patch(package) is True

    saved, extra = repo.update_and_rewrite(package_id, patch_documentation_slots)
    assert extra["created"] is True
    assert extra["patched"] == ["DigitalTwinCadModel", "DigitalTwinUsdModel"]
    assert documentation_needs_patch(saved) is False

    files = serialize_documentation(saved, lambda name: name)["submodels"][0]["files"]
    assert [(item["idShort"], item["missing"], item["fileName"], item["semanticId"]) for item in files] == [
        ("DigitalTwinCadModel", True, None, "urn:physical-ai:ids/cd/DigitalTwinCadModel/1/0"),
        ("DigitalTwinUsdModel", True, None, "urn:physical-ai:ids/cd/DigitalTwinUsdModel/1/0"),
    ]
    step = saved.objects.get(extra["submodelId"]).submodel_element.get("id_short", "DigitalTwinCadModel")
    assert isinstance(step, model.File)
    assert step.value is None
    assert step.semantic_id.key[0].value == "urn:physical-ai:ids/cd/DigitalTwinCadModel/1/0"


def test_patch_renames_step_alias_to_canonical(tmp_path: Path):
    files = aasx.DictSupplementaryFileContainer()
    documentation = model.Submodel(
        id_="https://example.org/sm/Documentation",
        id_short="Documentation",
        submodel_element=[
            model.File("step", content_type="application/step", value="/aasx/suppl/old.stp"),
        ],
    )
    files.add_file("/aasx/suppl/old.stp", __import__("io").BytesIO(b"ISO-STEP"), "application/step")
    shell = model.AssetAdministrationShell(
        id_="https://example.org/aas/demo",
        id_short="Demo",
        asset_information=model.AssetInformation(
            asset_kind=model.AssetKind.INSTANCE,
            global_asset_id="https://example.org/asset/demo",
        ),
        submodel={model.ModelReference.from_referable(documentation)},
    )
    _write_package(tmp_path, "demo", model.DictIdentifiableStore([shell, documentation]), files)

    repo = _repository(tmp_path)
    saved, extra = repo.update_and_rewrite("demo", patch_documentation_slots)
    assert extra["created"] is False
    assert extra["patched"] == ["DigitalTwinCadModel", "DigitalTwinUsdModel"]
    items = {item["idShort"]: item for item in serialize_documentation(saved, lambda n: n)["submodels"][0]["files"]}
    assert items["DigitalTwinCadModel"]["fileName"] == "old.stp"
    assert items["DigitalTwinCadModel"]["semanticId"] == "urn:physical-ai:ids/cd/DigitalTwinCadModel/1/0"
    assert items["DigitalTwinCadModel"]["missing"] is False
    assert items["DigitalTwinUsdModel"]["missing"] is True
    assert "step" not in items
    assert "STEP" not in items


def test_main_slot_files_are_copied_to_digital_twin_dirs(tmp_path: Path):
    repo, package_id = _repo_with_sample(tmp_path)

    saved, added = repo.update_and_rewrite(
        package_id,
        lambda pkg: apply_attachments(
            pkg,
            [
                Attachment("step", "cell.stp", b"ISO-10303-STEP"),
                Attachment("usd", "cell.usda", b"#usda 1.0\n"),
                Attachment("file", "mesh.glb", b"glTF", id_short="Mesh"),
            ],
            store_twin=lambda kind, filename, content: repo.write_digital_twin_file(
                pkg.id, kind, filename, content
            ),
        ),
    )
    by_id = {item["idShort"]: item for item in added["added"]}
    assert Path(by_id["DigitalTwinCadModel"]["diskPath"]).read_bytes() == b"ISO-10303-STEP"
    assert Path(by_id["DigitalTwinUsdModel"]["diskPath"]).read_bytes() == b"#usda 1.0\n"
    assert by_id["Mesh"]["diskPath"] is None
    assert (repo.layout.cad_dir(saved.id) / "cell.stp").is_file()
    assert (repo.layout.usd_dir(saved.id) / "cell.usda").is_file()

    repo.update_and_rewrite(
        package_id,
        lambda pkg: apply_attachments(
            pkg,
            [Attachment("step", "newer.stp", b"new-step")],
            store_twin=lambda kind, filename, content: repo.write_digital_twin_file(
                pkg.id, kind, filename, content
            ),
        ),
    )
    cad_dir = repo.layout.cad_dir(saved.id)
    assert not (cad_dir / "cell.stp").exists()
    assert (cad_dir / "newer.stp").read_bytes() == b"new-step"
