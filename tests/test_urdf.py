import math

import pytest

from app.urdf import read_urdf

MINIMAL = """<?xml version="1.0"?>
<robot name="mini">
  <link name="base_link"/>
  <link name="link1"/>
  <link name="link2"/>
  <joint name="j1" type="revolute">
    <parent link="base_link"/>
    <child link="link1"/>
    <origin xyz="0 0 0.2" rpy="0 0 0"/>
    <axis xyz="0 0 1"/>
    <limit lower="-1.5" upper="1.5"/>
  </joint>
  <joint name="j2" type="revolute">
    <parent link="link1"/>
    <child link="link2"/>
    <origin xyz="0.1 0 0" rpy="0 0 0"/>
    <axis xyz="0 1 0"/>
    <limit lower="-2" upper="2"/>
  </joint>
</robot>
"""


def test_read_urdf_world_pivots():
    parsed = read_urdf(MINIMAL)
    assert parsed["robot"] == "mini"
    assert parsed["base_links"] == ["base_link"]
    assert [item["name"] for item in parsed["joints"]] == ["j1", "j2"]
    assert parsed["joints"][0]["pivot"] == pytest.approx([0, 0, 0.2])
    assert parsed["joints"][0]["axis"] == pytest.approx([0, 0, 1])
    assert parsed["joints"][1]["pivot"] == pytest.approx([0.1, 0, 0.2])
    assert parsed["joints"][1]["axis"] == pytest.approx([0, 1, 0])
    assert parsed["joints"][0]["lower"] == pytest.approx(-1.5)
    assert parsed["joints"][1]["upper"] == pytest.approx(2)


def test_read_urdf_rejects_dtd():
    with pytest.raises(ValueError, match="DTD"):
        read_urdf("<!DOCTYPE robot [ <!ENTITY x SYSTEM 'x'> ]><robot name='x'></robot>")


def test_read_urdf_requires_robot():
    with pytest.raises(ValueError, match="robot"):
        read_urdf("<not-robot/>")
    assert math.isfinite(read_urdf(MINIMAL)["joints"][0]["zeroQuaternion"][3])
