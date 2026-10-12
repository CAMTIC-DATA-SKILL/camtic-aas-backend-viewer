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

# 설정 파일 생성 후 AASX_DATA_DIR 등 수정
Copy-Item .env.example .env

# (선택) 테스트용 샘플 AASX 생성 → AASX_DATA_DIR
python -m scripts.create_sample_aasx

# 서버 실행 (호스트/포트 등은 .env 설정을 따름)
python -m app
```

- 뷰어: http://127.0.0.1:8000/
- API 문서(Swagger): http://127.0.0.1:8000/docs

## 설정 (.env)

모든 설정은 레포 루트의 `.env` 에서 읽으며, 코드에서는 `config.py` 의 `settings` 만 참조합니다.
OS 환경 변수가 이미 설정되어 있으면 `.env` 값보다 우선합니다. 상대 경로는 레포 루트 기준입니다.

| 변수 | 설명 | 기본값 |
| --- | --- | --- |
| `AASX_DATA_DIR` | AASX 파일 폴더 | **필수** |
| `SERVER_HOST` | 바인딩 주소 | `127.0.0.1` |
| `SERVER_PORT` | 포트 | `8000` |
| `SERVER_RELOAD` | 코드 변경 시 자동 재시작 (개발용) | `false` |
| `CORS_ORIGINS` | 허용할 Origin, 콤마 구분 | `*` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` | `INFO` |
| `LOG_DIR` | 로그 파일 폴더, 비우면 콘솔에만 출력 | (없음) |
| `LOG_FILE_NAME` | 로그 파일 이름 | `app.log` |
| `LOG_MAX_BYTES` | 로그 파일 회전 크기 | `10485760` |
| `LOG_BACKUP_COUNT` | 보관할 이전 로그 파일 수 | `5` |

## 로그

로그 설정은 `utils/log_setup.py` 의 `LogSetup` 에서 정의합니다. 새 모듈에서는 다음과 같이 로거를 얻습니다.

```python
from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)
```

uvicorn, BaSyx SDK 로그도 같은 형식으로 콘솔과 로그 파일에 함께 기록됩니다.

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
config.py          .env / 환경 변수 → settings
.env.example       .env 템플릿
utils/
  log_setup.py     공용 로그 설정 (LogSetup)
app/
  __main__.py      서버 진입점 (python -m app)
  main.py          FastAPI 앱 생성, CORS, 정적 뷰어 마운트
  api.py           REST 엔드포인트
  repository.py    폴더 내 AASX 로드 (basyx AASXReader)
  serializers.py   간소화 트리 변환
  static/          HTML 트리 뷰어
scripts/
  create_sample_aasx.py  샘플 AASX 생성 (python -m scripts.create_sample_aasx)
```
