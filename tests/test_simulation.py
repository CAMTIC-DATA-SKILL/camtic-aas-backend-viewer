from pathlib import Path

import pytest

from app.documentation import Attachment, apply_attachments
from app.repository import AasxRepository
from app.simulation import build_simulation_payload, has_robot_kinematics
from app.step_mesh import convert_step_to_glb, glb_part_names
from config import resolve_storage
from scripts.create_sample_aasx import build_camtic_sample, write_aasx

HH7_STEP = (
    Path(__file__).resolve().parents[1]
    / "data/packages/HH7_AAS_Template_v0.2/cad/hh7_v3.step"
)
SIM_STEP = Path("/home/user/workspace/camtic-digital-simulator/assets/hh7_v3.step")


def _repo(tmp_path: Path) -> tuple[AasxRepository, str]:
    layout = resolve_storage(tmp_path)
    dest = layout.aasx_path("camtic_sample")
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_aasx(dest, *build_camtic_sample())
    repo = AasxRepository(layout)
    repo.reload()
    return repo, "camtic_sample"


def test_sample_package_has_no_simulation_mesh(tmp_path: Path):
    repo, package_id = _repo(tmp_path)
    package = repo.get(package_id)
    payload = build_simulation_payload(package, repo)
    assert payload["hasMesh"] is False
    assert payload["meshUrl"] is None
    assert payload["hasKinematics"] is False
    assert payload["joints"] == []


def _hh7_step() -> Path:
    source = HH7_STEP if HH7_STEP.is_file() else SIM_STEP
    if not source.is_file():
        pytest.skip("HH7 STEP 파일이 없습니다.")
    return source


def test_payload_uses_named_step_parts(tmp_path: Path):
    source = _hh7_step()
    repo, package_id = _repo(tmp_path)
    content = source.read_bytes()
    repo.update_and_rewrite(
        package_id,
        lambda pkg: apply_attachments(
            pkg,
            [Attachment("step", "hh7_v3.step", content)],
            store_twin=lambda kind, filename, body: repo.write_digital_twin_file(pkg.id, kind, filename, body),
        ),
    )
    package = repo.get(package_id)
    payload = build_simulation_payload(package, repo, root_path="")
    assert payload["hasMesh"] is True
    assert payload["cadFile"] == "hh7_v3.step"
    assert payload["articulated"] is True
    assert payload["kinematicsSource"] == "suggested"
    assert payload["meshUrl"] == f"/api/packages/{package_id}/simulation/mesh"
    assert payload["parts"][:7] == ["base_link", "link1", "link2", "link3", "link4", "link5", "link6"]
    assert len(payload["joints"]) == 6
    assert (repo.layout.cad_dir(package_id) / "hh7_v3.glb").is_file()


def test_convert_preserves_link_names(tmp_path: Path):
    source = _hh7_step()
    dest = tmp_path / "robot.glb"
    info = convert_step_to_glb(source, dest)
    assert info["parts"][:7] == ["base_link", "link1", "link2", "link3", "link4", "link5", "link6"]
    assert glb_part_names(dest)[:7] == ["base_link", "link1", "link2", "link3", "link4", "link5", "link6"]
    import json
    import struct

    data = dest.read_bytes()
    chunk_len, chunk_type = struct.unpack_from("<II", data, 12)
    meta = json.loads(data[20 : 20 + chunk_len])
    node_names = [(node or {}).get("name") for node in meta.get("nodes") or []]
    mesh_names = [(mesh or {}).get("name") for mesh in meta.get("meshes") or []]
    assert "base_link:1" not in node_names
    assert set(mesh_names) >= {"base_link", "link1", "link6"}


def test_has_robot_kinematics_false_for_sample(tmp_path: Path):
    repo, package_id = _repo(tmp_path)
    assert has_robot_kinematics(repo.get(package_id)) is False
