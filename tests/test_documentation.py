from pathlib import Path

import pytest
from basyx.aas import model
from basyx.aas.adapter import aasx

from app.documentation import Attachment, apply_attachments, serialize_documentation
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
    assert files["STEP"]["kind"] == "step"
    assert files["STEP"]["fileName"] == "cell.stp"
    assert files["USD"]["kind"] == "usd"
    assert files["mesh"]["kind"] == "file"
    assert files["STEP"]["url"] == "/files/aasx/suppl/cell.stp"

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
    kinds = {item["kind"] for item in docs["submodels"][0]["files"]}
    assert kinds == {"file", "usd"}


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

    package = repo.get(package_id)
    with pytest.raises(ValueError, match="STEP 규격"):
        apply_attachments(package, [Attachment("step", "notes.pdf", b"%PDF", id_short="Manual")])
