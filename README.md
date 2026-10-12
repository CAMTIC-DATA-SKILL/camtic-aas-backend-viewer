# camtic-aas-backend-viewer
AAS 표준 템플릿을 Python Eclipse BaSyx SDK를 활용하여 자동 파싱하고 Front로 서빙하는 목적으로 생성된 레포입니다.

지정한 폴더의 모든 `.aasx` 파일을 시작 시 메모리에 로드하고, FastAPI로 다음을 제공합니다.

- 표준 AAS v3 JSON (BaSyx SDK 직렬화 그대로)
- 프론트엔드 렌더링용 간소화 트리 JSON
- AASX 내부 첨부 파일 / 썸네일 다운로드
- 바로 확인 가능한 HTML 트리 뷰어 (`/`)

## 실행

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# (선택) 테스트용 샘플 AASX 생성 → data/
python scripts/create_sample_aasx.py

# 서버 실행 (기본: ./data 폴더, http://127.0.0.1:8000)
python -m app
python -m app --data-dir D:\aasx --host 0.0.0.0 --port 8000
python -m app --reload   # 개발용 자동 재시작
```

- 뷰어: http://127.0.0.1:8000/
- API 문서(Swagger): http://127.0.0.1:8000/docs

| 환경변수 | 설명 | 기본값 |
| --- | --- | --- |
| `AASX_DATA_DIR` | AASX 파일 폴더 (`--data-dir` 가 우선) | `data` |
| `AASX_CORS_ORIGINS` | 허용할 Origin, 콤마 구분 | `*` |

폴더에 파일을 추가/교체한 뒤에는 뷰어의 "폴더 다시 읽기" 버튼 또는 `POST /api/packages/reload` 로 다시 로드합니다.

## API

`{package_id}` 는 AASX 파일명(확장자 제외)입니다. AAS/Submodel/ConceptDescription id 는
AAS Part 2 API와 동일하게 **base64url 인코딩**해서 전달합니다. 트리 응답의 `idBase64` 값을 그대로 쓰면 됩니다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/packages` | 로드된 패키지 목록과 요약 |
| POST | `/api/packages/reload` | 폴더 재스캔 |
| GET | `/api/packages/{package_id}` | 패키지 요약 (메타데이터, 개수, 로드 오류) |
| GET | `/api/packages/{package_id}/tree` | 간소화 트리 (AAS → Submodel → Element) |
| GET | `/api/packages/{package_id}/environment` | 표준 AAS JSON Environment 전체 |
| GET | `/api/packages/{package_id}/shells/{aas_id}` | AAS 표준 JSON |
| GET | `/api/packages/{package_id}/submodels/{submodel_id}` | Submodel 표준 JSON |
| GET | `/api/packages/{package_id}/submodels/{submodel_id}/tree` | Submodel 간소화 트리 |
| GET | `/api/packages/{package_id}/concept-descriptions/{cd_id}` | ConceptDescription 표준 JSON |
| GET | `/api/packages/{package_id}/thumbnail` | 패키지 썸네일 |
| GET | `/api/packages/{package_id}/files/{path}` | AASX 내부 첨부 파일 (File 요소의 `url`) |

### 간소화 트리 형식

```jsonc
{
  "package": { "id": "camtic_sample", "fileName": "camtic_sample.aasx", "counts": { ... } },
  "shells": [
    {
      "modelType": "AssetAdministrationShell",
      "id": "...", "idBase64": "...", "idShort": "CNCMachine001",
      "assetInformation": { "assetKind": "Instance", "globalAssetId": "...", "thumbnail": { "url": "..." } },
      "submodels": [
        {
          "modelType": "Submodel", "id": "...", "idShort": "Nameplate", "semanticId": "...",
          "children": [
            { "modelType": "MultiLanguageProperty", "path": "ManufacturerName",
              "value": { "ko": "캠틱종합기술원", "en": "CAMTIC" }, "conceptId": "0173-1#02-AAO677#002" },
            { "modelType": "Property", "path": "SerialNumber", "valueType": "xs:string", "value": "CNC-2024-0001" },
            { "modelType": "SubmodelElementCollection", "path": "ContactInformation", "children": [ ... ] }
          ]
        }
      ]
    }
  ],
  "orphanSubmodels": [ ... ],            // 어떤 AAS 에도 연결되지 않은 Submodel
  "conceptDescriptions": { "<id>": { "preferredName": { ... }, "unit": "...", ... } }
}
```

- `path` 는 Submodel 기준 idShortPath 입니다 (리스트 요소는 `Axes[0].Name` 형태).
- 값이 없는 필드는 응답에서 생략됩니다.
- `conceptId` 가 있으면 `conceptDescriptions[conceptId]` 에서 IEC 61360 이름/정의/단위를 찾을 수 있습니다.

## 구조

```
app/
  __main__.py      CLI 진입점 (python -m app)
  main.py          FastAPI 앱 생성, CORS, 정적 뷰어 마운트
  api.py           REST 엔드포인트
  repository.py    폴더 내 AASX 로드 (basyx AASXReader)
  serializers.py   간소화 트리 변환
  config.py        환경변수 설정
  static/          HTML 트리 뷰어
scripts/
  create_sample_aasx.py  샘플 AASX 생성
```
