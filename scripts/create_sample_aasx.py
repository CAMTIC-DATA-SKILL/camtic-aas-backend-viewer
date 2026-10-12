"""뷰어 테스트용 샘플 AASX 파일을 생성한다.

사용법:
    python scripts/create_sample_aasx.py [출력 폴더(기본값: data)]

생성 파일:
    camtic_sample.aasx        한글/영문 다국어 값, 첨부 파일, 썸네일이 포함된 간단한 예제
    basyx_full_example.aasx   BaSyx SDK가 제공하는 전체 메타모델 예제 (모든 SubmodelElement 종류 포함)
"""

import io
import sys
from pathlib import Path

from basyx.aas import model
from basyx.aas.adapter import aasx
from basyx.aas.examples.data import example_aas

THUMBNAIL_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
<rect width="120" height="120" rx="12" fill="#1b2b45"/>
<text x="60" y="70" font-size="28" fill="#fff" text-anchor="middle" font-family="sans-serif">CNC</text>
</svg>"""

MANUAL_TEXT = "CNC 가공기 사용 설명서 (샘플)\n\n1. 전원을 켭니다.\n2. 원점 복귀를 수행합니다.\n".encode("utf-8")


def _semantic(value: str) -> model.ExternalReference:
    return model.ExternalReference((model.Key(model.KeyTypes.GLOBAL_REFERENCE, value),))


def _concept(id_: str, id_short: str, name_ko: str, name_en: str, unit: str | None = None) -> model.ConceptDescription:
    return model.ConceptDescription(
        id_=id_,
        id_short=id_short,
        embedded_data_specifications=[
            model.EmbeddedDataSpecification(
                data_specification=_semantic(
                    "https://admin-shell.io/DataSpecificationTemplates/DataSpecificationIEC61360/3/0"
                ),
                data_specification_content=model.DataSpecificationIEC61360(
                    preferred_name=model.PreferredNameTypeIEC61360({"ko": name_ko, "en": name_en}),
                    definition=model.DefinitionTypeIEC61360({"ko": f"{name_ko}에 대한 정의", "en": f"Definition of {name_en}"}),
                    unit=unit,
                ),
            )
        ],
    )


def build_camtic_sample() -> tuple[model.DictIdentifiableStore, aasx.DictSupplementaryFileContainer]:
    files = aasx.DictSupplementaryFileContainer()
    thumbnail_name = files.add_file("/aasx/thumbnail.svg", io.BytesIO(THUMBNAIL_SVG), "image/svg+xml")
    manual_name = files.add_file("/aasx/suppl/manual.txt", io.BytesIO(MANUAL_TEXT), "text/plain")

    cd_manufacturer = _concept("0173-1#02-AAO677#002", "ManufacturerName", "제조사명", "Manufacturer name")
    cd_spindle = _concept("https://camtic.or.kr/cd/MaxSpindleSpeed", "MaxSpindleSpeed", "최대 주축 회전수", "Max spindle speed", "1/min")

    nameplate = model.Submodel(
        id_="https://camtic.or.kr/aas/sample/sm/Nameplate",
        id_short="Nameplate",
        semantic_id=_semantic("https://admin-shell.io/idta/nameplate/3/0/Nameplate"),
        administration=model.AdministrativeInformation(version="3", revision="0"),
        submodel_element=[
            model.MultiLanguageProperty(
                "ManufacturerName",
                value=model.MultiLanguageTextType({"ko": "캠틱종합기술원", "en": "CAMTIC"}),
                semantic_id=_semantic(cd_manufacturer.id),
            ),
            model.MultiLanguageProperty(
                "ManufacturerProductDesignation",
                value=model.MultiLanguageTextType({"ko": "5축 CNC 가공기", "en": "5-axis CNC machine"}),
            ),
            model.Property("SerialNumber", model.datatypes.String, value="CNC-2024-0001"),
            model.Property("YearOfConstruction", model.datatypes.String, value="2024"),
            model.SubmodelElementCollection(
                "ContactInformation",
                value=[
                    model.MultiLanguageProperty(
                        "Street", value=model.MultiLanguageTextType({"ko": "전주시 덕진구 유상로 67", "en": "67 Yusang-ro, Jeonju"})
                    ),
                    model.Property("Phone", model.datatypes.String, value="+82-63-219-0300"),
                ],
            ),
        ],
    )

    technical_data = model.Submodel(
        id_="https://camtic.or.kr/aas/sample/sm/TechnicalData",
        id_short="TechnicalData",
        semantic_id=_semantic("https://admin-shell.io/ZVEI/TechnicalData/Submodel/1/2"),
        submodel_element=[
            model.Property(
                "MaxSpindleSpeed",
                model.datatypes.Int,
                value=12000,
                semantic_id=_semantic(cd_spindle.id),
            ),
            model.Range("OperatingTemperature", model.datatypes.Double, min=5.0, max=40.0),
            model.SubmodelElementList(
                "Axes",
                type_value_list_element=model.SubmodelElementCollection,
                value=[
                    model.SubmodelElementCollection(
                        None,
                        value=[
                            model.Property("Name", model.datatypes.String, value=axis),
                            model.Property("TravelMm", model.datatypes.Double, value=travel),
                        ],
                    )
                    for axis, travel in (("X", 800.0), ("Y", 600.0), ("Z", 500.0))
                ],
            ),
            model.File("Manual", content_type="text/plain", value=manual_name),
        ],
    )

    operational_data = model.Submodel(
        id_="https://camtic.or.kr/aas/sample/sm/OperationalData",
        id_short="OperationalData",
        submodel_element=[model.Property("OperatingHours", model.datatypes.Double, value=1234.5)],
    )

    shell = model.AssetAdministrationShell(
        id_="https://camtic.or.kr/aas/sample/CNCMachine001",
        id_short="CNCMachine001",
        display_name=model.MultiLanguageNameType({"ko": "CNC 가공기 1호기", "en": "CNC machine #1"}),
        asset_information=model.AssetInformation(
            asset_kind=model.AssetKind.INSTANCE,
            global_asset_id="https://camtic.or.kr/asset/CNCMachine001",
            default_thumbnail=model.Resource(thumbnail_name, "image/svg+xml"),
        ),
        submodel={model.ModelReference.from_referable(nameplate), model.ModelReference.from_referable(technical_data)},
    )

    store = model.DictIdentifiableStore(
        [shell, nameplate, technical_data, operational_data, cd_manufacturer, cd_spindle]
    )
    return store, files


def build_full_example() -> tuple[model.DictIdentifiableStore, aasx.DictSupplementaryFileContainer]:
    files = aasx.DictSupplementaryFileContainer()
    test_pdf = Path(example_aas.__file__).parent / "TestFile.pdf"
    with open(test_pdf, "rb") as f:
        files.add_file("/TestFile.pdf", f, "application/pdf")
    return example_aas.create_full_example(), files


def write_aasx(path: Path, store: model.DictIdentifiableStore, files: aasx.DictSupplementaryFileContainer,
               thumbnail: bytes | None = None) -> None:
    with aasx.AASXWriter(path) as writer:
        writer.write_all_aas_objects("/aasx/data.json", store, files)
        if thumbnail:
            writer.write_thumbnail("/thumbnail.svg", bytearray(thumbnail), "image/svg+xml")
    print(f"created {path}")


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
    out_dir.mkdir(parents=True, exist_ok=True)
    write_aasx(out_dir / "camtic_sample.aasx", *build_camtic_sample(), thumbnail=THUMBNAIL_SVG)
    write_aasx(out_dir / "basyx_full_example.aasx", *build_full_example())


if __name__ == "__main__":
    main()
