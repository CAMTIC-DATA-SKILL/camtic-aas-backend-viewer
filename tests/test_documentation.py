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
from scripts.create_sample_aasx import build_camtic_sample, write_aasx


def _repo_with_sample(tmp_path: Path) -> tuple[AasxRepository, str]:
    data_dir = tmp_path / "aasx"
    data_dir.mkdir()
    write_aasx(data_dir / "camtic_sample.aasx", *build_camtic_sample())
    repo = AasxRepository(data_dir, tmp_path / "artifacts")
    repo.reload()
    return repo, "camtic_sample"


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
    data_dir = tmp_path / "aasx"
    data_dir.mkdir()
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
    write_aasx(data_dir / "demo.aasx", store, files)

    repo = AasxRepository(data_dir, tmp_path / "artifacts")
    repo.reload()
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
    data_dir = tmp_path / "aasx"
    data_dir.mkdir()
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
    write_aasx(data_dir / "demo.aasx", model.DictIdentifiableStore([shell, documentation]), files)

    repo = AasxRepository(data_dir, tmp_path / "artifacts")
    repo.reload()
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
