"""URDF 6축 관절을 월드 좌표 피벗·축으로 변환한다."""

from __future__ import annotations

import math
from typing import Any
from xml.etree import ElementTree as ET


def rpy_matrix(rpy: list[float]) -> list[list[float]]:
    r, p, y = rpy
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def _mv(m: list[list[float]], v: list[float]) -> list[float]:
    return [sum(m[i][k] * v[k] for k in range(3)) for i in range(3)]


def _mm(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def matrix_to_quaternion(m: list[list[float]]) -> list[float]:
    t = m[0][0] + m[1][1] + m[2][2]
    if t > 0:
        s = 2 * math.sqrt(t + 1)
        q = [(m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, s / 4]
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = 2 * math.sqrt(max(0, 1 + m[0][0] - m[1][1] - m[2][2]))
        q = [s / 4, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s]
    elif m[1][1] > m[2][2]:
        s = 2 * math.sqrt(max(0, 1 + m[1][1] - m[0][0] - m[2][2]))
        q = [(m[0][1] + m[1][0]) / s, s / 4, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s]
    else:
        s = 2 * math.sqrt(max(0, 1 + m[2][2] - m[0][0] - m[1][1]))
        q = [(m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, s / 4, (m[1][0] - m[0][1]) / s]
    n = math.sqrt(sum(v * v for v in q)) or 1
    return [v / n for v in q]


def _triple(raw: str | None, default: list[float]) -> list[float]:
    if not raw:
        return default[:]
    vals = [float(v) for v in raw.replace(",", " ").split()]
    if len(vals) != 3 or not all(math.isfinite(v) for v in vals):
        raise ValueError("URDF xyz/rpy/axis 값은 유한한 숫자 3개여야 합니다.")
    return vals


def read_urdf(xml_text: str) -> dict[str, Any]:
    if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
        raise ValueError("XML DTD/ENTITY 구문은 허용하지 않습니다.")
    root = ET.fromstring(xml_text)
    if root.tag != "robot":
        raise ValueError("URDF <robot> 최상위 태그가 없습니다. xacro는 사전 변환이 필요합니다.")

    definitions: list[dict[str, Any]] = []
    for joint in root.findall("joint"):
        parent = joint.find("parent")
        child = joint.find("child")
        if parent is None or child is None:
            continue
        origin = joint.find("origin")
        axis = joint.find("axis")
        limit = joint.find("limit")
        definitions.append(
            {
                "name": joint.get("name", ""),
                "type": joint.get("type", "fixed"),
                "parent": parent.get("link", ""),
                "child": child.get("link", ""),
                "xyz": _triple(origin.get("xyz") if origin is not None else "", [0, 0, 0]),
                "rpy": _triple(origin.get("rpy") if origin is not None else "", [0, 0, 0]),
                "axis": _triple(axis.get("xyz") if axis is not None else "", [1, 0, 0]),
                "lower": float(limit.get("lower", "-3.14159265")) if limit is not None else -math.pi,
                "upper": float(limit.get("upper", "3.14159265")) if limit is not None else math.pi,
            }
        )

    children = {item["child"] for item in definitions}
    parents = {item["parent"] for item in definitions}
    roots = sorted(parents - children)
    if not roots:
        raise ValueError("URDF의 기본 링크(base link)를 찾을 수 없습니다.")

    ident = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    transforms = {link: ([0, 0, 0], ident) for link in roots}
    pending = definitions[:]
    results: list[dict[str, Any]] = []
    for _ in range(len(definitions) + 1):
        if not pending:
            break
        rest: list[dict[str, Any]] = []
        progress = False
        for item in pending:
            if item["parent"] not in transforms:
                rest.append(item)
                continue
            progress = True
            point, rot = transforms[item["parent"]]
            worldp = [a + b for a, b in zip(point, _mv(rot, item["xyz"]))]
            worldr = _mm(rot, rpy_matrix(item["rpy"]))
            transforms[item["child"]] = (worldp, worldr)
            if item["type"] in ("revolute", "continuous", "prismatic"):
                axis = _mv(worldr, item["axis"])
                norm = math.sqrt(sum(a * a for a in axis)) or 1.0
                results.append(
                    {
                        "name": item["name"],
                        "type": item["type"],
                        "parent": item["parent"],
                        "child": item["child"],
                        "pivot": worldp,
                        "axis": [a / norm for a in axis],
                        "zeroQuaternion": matrix_to_quaternion(worldr),
                        "lower": item["lower"],
                        "upper": item["upper"],
                    }
                )
        if not progress:
            break
        pending = rest

    return {
        "robot": root.get("name", "robot"),
        "base_links": roots,
        "joints": results,
        "warnings": [
            "관절축과 child 링크 0도 자세를 URDF의 월드 좌표로 변환합니다.",
            "STEP과 URDF의 기준 좌표계 및 기본 자세 정합은 별도 검증해야 합니다.",
        ],
    }
