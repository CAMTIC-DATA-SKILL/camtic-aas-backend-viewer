# camtic-aas-backend-viewer

AAS 표준 템플릿을 Python Eclipse BaSyx SDK로 파싱하고, AASX 패키지 관리·뷰어·OPC-UA 산출물 생성·현장 OPC-UA 서버 조회를 제공하는 데이터 플랫폼입니다.

시작 시 지정 폴더의 `.aasx` 파일을 메모리에 로드하고, FastAPI로 다음을 제공합니다.

- 데이터 플랫폼 홈 (`/`) — AASX 패키지 목록, 업로드/다운로드
- HTML 트리 뷰어 (`/viewer.html`) — 패키지 클릭 시 AAS 구조 탐색, **실시간 그래프**
- 표준 AAS v3 JSON (BaSyx SDK 직렬화)
- AASX 업로드 시 OPC-UA용 `nodeset.xml`, `engineering.csv`, `syscfg.json` 자동 생성
- AASX / OPC-UA 산출물 다운로드
- 현장 OPC-UA 서버 클라이언트 (접속 · browse · 읽기/쓰기 · 구독)

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

- 플랫폼: http://127.0.0.1:8000/
- 뷰어: http://127.0.0.1:8000/viewer.html?package={package_id}
- API 문서(Swagger): http://127.0.0.1:8000/docs

## 설정 (.env)

모든 설정은 레포 루트의 `.env` 에서 읽으며, 코드에서는 `config.py` 의 `settings` 만 참조합니다.
OS 환경 변수가 이미 설정되어 있으면 `.env` 값보다 우선합니다. 상대 경로는 레포 루트 기준입니다.

| 변수 | 설명 | 기본값 |
| --- | --- | --- |
| `AASX_DATA_DIR` | AASX 파일 폴더 | **필수** |
| `ARTIFACTS_DIR` | OPC-UA 산출물 폴더 | `AASX_DATA_DIR`의 형제 `artifacts` |
| `SERVER_HOST` | 바인딩 주소 | `127.0.0.1` |
| `SERVER_PORT` | 포트 | `8000` |
| `SERVER_RELOAD` | 코드 변경 시 자동 재시작 (개발용) | `false` |
| `CORS_ORIGINS` | 허용할 Origin, 콤마 구분 | `*` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` | `INFO` |
| `LOG_DIR` | 로그 파일 폴더, 비우면 콘솔에만 출력 | (없음) |
| `LOG_FILE_NAME` | 로그 파일 이름 | `app.log` |
| `LOG_MAX_BYTES` | 로그 파일 회전 크기 | `10485760` |
| `LOG_BACKUP_COUNT` | 보관할 이전 로그 파일 수 | `5` |
| `OPCUA_ENDPOINT` | 현장 OPC-UA 서버 주소 (카탈로그가 없을 때 기본값) | `opc.tcp://127.0.0.1:4840` |
| `OPCUA_USERNAME` / `OPCUA_PASSWORD` | 인증 (비우면 Anonymous) | (없음) |
| `OPCUA_SECURITY_POLICY` | `None` 또는 보안 정책 | `None` |
| `OPCUA_CONNECT_MAX_RETRIES` | 최초 접속 재시도 횟수 (`0` = 무제한) | `3` |
| `OPCUA_SUBSCRIPTION_PERIOD_MS` | 구독 publishing 주기 | `500` |
| `COLLECTOR_URL` | HDF7-9 수집기 HTTP 기본 주소 | `http://127.0.0.1:4100` |
| `RECORD_DIR` | 실시간 REC CSV 폴더 | `data/records` |
| `RECORD_INTERVAL` | REC 기록 간격(초) | `1.0` |

## 로그

로그 설정은 `utils/log_setup.py` 의 `LogSetup` 에서 정의합니다. 새 모듈에서는 다음과 같이 로거를 얻습니다.

```python
from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)
```

uvicorn, BaSyx SDK 로그도 같은 형식으로 콘솔과 로그 파일에 함께 기록됩니다.

폴더에 파일을 추가/교체한 뒤에는 플랫폼의 "목록 새로고침" 또는 `POST /api/packages/reload` 로 다시 로드합니다.

## 플랫폼 사용

1. `/` 에서 AASX 패키지 목록을 확인합니다.
2. **AAS 데이터 업로드**로 `.aasx` 파일을 올리면 서버가 패키지를 저장·파싱하고 OPC-UA 산출물을 생성합니다.
3. 행을 클릭하면 해당 패키지의 AAS 트리 뷰어로 이동합니다. **실시간 그래프**는 매핑된 AAS Property를 시계열로 그립니다.
4. 라디오로 패키지를 선택한 뒤 **AAS 데이터 다운로드**(원본 AASX), **OPC-UA 산출물 다운로드**(ZIP), 또는 **삭제**를 사용할 수 있습니다.

### OPC-UA 산출물

업로드(또는 최초 로드) 시 패키지별로 `ARTIFACTS_DIR/{package_id}/` 아래에 다음이 생성됩니다.

| 파일 | 설명 |
| --- | --- |
| `nodeset.xml` | OPC UA NodeSet2 XML (AAS Shell/Submodel/Property → Object/Variable) |
| `engineering.csv` | 노드·AAS 경로·semanticId·값 매핑 엔지니어링 테이블 |
| `syscfg.json` | 네임스페이스, 엔드포인트 템플릿, AAS 요약 시스템 설정 |

매핑은 AAS V3 / IDTA 구조와 OPC UA Companion Spec for AAS(OPC 30270, I4AAS) 개념을 따릅니다.

### OPC-UA 클라이언트

`middleware_gateway_opc_ua` 와 같은 `asyncua` 래퍼입니다. 유틸리티 → OPC-UA에서 카탈로그 서버를 고르고 접속하면 주소 공간을 탐색하고, 매핑된 NodeId를 한 번에 구독합니다. 패키지를 선택한 뒤 **선택 패키지 AAS 매핑 읽기**를 누르면 `data/mappings/{package_id}.json` (또는 산출물 폴더의 `mapping.json`)에 정의된 AAS Property를 현장 NodeId로 읽어 옵니다. 뷰어의 **현장 값 불러오기**도 같은 매핑을 트리에 덮어씁니다. **실시간 그래프**는 구독 캐시를 0.5초마다 읽어 차트에 올립니다.

서버 목록은 `data/opcua/catalog.json` 에 두고, 현장 주소는 git에 올리지 않는 `data/opcua/catalog.local.json` 으로 덮어씁니다. 카탈로그에는 엔드포인트·보안 정책·(선택) `aasId` 만 적습니다. 구독 토픽은 그 AAS 산출물 `engineering.csv` 의 Variable NodeId 입니다. 미들웨어가 같은 NodeId를 올리면 됩니다.

서버 시작 시 카탈로그 기본 서버에 자동 접속하고, 해당 AAS 산출물 `engineering.csv` 의 Variable NodeId를 한 번에 구독합니다. 미들웨어가 그 NodeId를 구현하면 `mappings/*.json` 없이 AAS 경로와 값이 맞춰집니다. 뷰어 **실시간 그래프**는 `GET /api/opcua/packages/{package_id}/live` 로 이 캐시를 읽습니다.

클라이언트 모듈은 `app/opc_ua/client/` 에 있고, HTTP 요청 사이 연결은 `OpcUaSession` 이 유지합니다.

### 실시간 그래프

뷰어에서 **실시간 그래프**를 누르면 AAS 트리를 유지한 채, 매핑된 Property를 [HDF7-9](https://github.com/CAMTIC-DATA-SKILL/HDF7-9_data_collector)처럼 시계열로 그립니다.

- 시리즈 키는 AAS 경로 (`OperationalData/ActualDataMonitoring.EquipmentState.PowerYesno`)
- 트리의 LIVE 속성을 클릭하면 그래프 표시 on/off, GRAPH 배지로 표시
- JSON은 AAS 스냅샷(서브모델 → 컬렉션 → Property)
- Joint / Cartesian 차트는 해당 AAS 경로가 매핑된 경우에만 나타남
- 시간 범위: 60초 / 10분 / 30분 / 1시간
- **REC**: 1초 간격으로 `data/records/{package_id}/YYYY-MM-DD-data.csv` 에 AAS 경로 컬럼으로 기록

유틸리티 → OPC-UA에서 카탈로그 서버를 고르고 접속한 뒤 사용합니다. 차트는 Chart.js CDN을 쓰므로 브라우저에 인터넷이 필요합니다.

## API

`{package_id}` 는 AASX 파일명(확장자 제외)입니다. AAS/Submodel/ConceptDescription id 는
AAS Part 2 API와 동일하게 **base64url 인코딩**해서 전달합니다. 트리 응답의 `idBase64` 값을 그대로 쓰면 됩니다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/packages` | 로드된 패키지 목록과 요약 |
| POST | `/api/packages` | AASX 업로드 + OPC-UA 산출물 생성 (`multipart/form-data`, 필드명 `file`) |
| POST | `/api/packages/reload` | 폴더 재스캔 |
| GET | `/api/packages/{package_id}` | 패키지 요약 |
| DELETE | `/api/packages/{package_id}` | AASX 패키지 + OPC-UA 산출물 삭제 |
| GET | `/api/packages/{package_id}/download` | 원본 AASX 다운로드 |
| GET | `/api/packages/{package_id}/tree` | 간소화 트리 (AAS → Submodel → Element) |
| GET | `/api/packages/{package_id}/environment` | 표준 AAS JSON Environment 전체 |
| GET | `/api/packages/{package_id}/shells/{aas_id}` | AAS 표준 JSON |
| GET | `/api/packages/{package_id}/submodels/{submodel_id}` | Submodel 표준 JSON |
| GET | `/api/packages/{package_id}/submodels/{submodel_id}/tree` | Submodel 간소화 트리 |
| GET | `/api/packages/{package_id}/concept-descriptions/{cd_id}` | ConceptDescription 표준 JSON |
| GET | `/api/packages/{package_id}/thumbnail` | 패키지 썸네일 |
| GET | `/api/packages/{package_id}/files/{path}` | AASX 내부 첨부 파일 |
| GET | `/api/packages/{package_id}/artifacts` | OPC-UA 산출물 목록 |
| POST | `/api/packages/{package_id}/artifacts/generate` | OPC-UA 산출물 재생성 |
| GET | `/api/packages/{package_id}/artifacts/download` | 산출물 ZIP 다운로드 |
| GET | `/api/packages/{package_id}/artifacts/{name}` | 개별 산출물 다운로드 (`nodeset.xml` 등) |
| GET | `/api/shells/{aas_id}` | AAS id(URN/IRI)로 셸·패키지 요약 |
| GET | `/api/shells/{aas_id}/artifacts` | AAS id로 OPC-UA 산출물 목록 |
| POST | `/api/shells/{aas_id}/artifacts/generate` | AAS id로 산출물 재생성 |
| GET | `/api/shells/{aas_id}/artifacts/download` | AAS id로 산출물 ZIP |
| GET | `/api/shells/{aas_id}/artifacts/{name}` | AAS id로 개별 산출물 (`nodeset.xml` 등) |
| GET | `/api/opcua/status` | OPC-UA 클라이언트 연결 상태 · 구독 목록 |
| GET | `/api/opcua/catalog` | OPC-UA 서버 카탈로그 |
| POST | `/api/opcua/connect` | 현장 서버 접속 (`serverId` 또는 `endpoint`, `aasId` 있으면 해당 AAS 매핑 전체 구독) |
| POST | `/api/opcua/disconnect` | 연결 종료 |
| GET | `/api/opcua/namespaces` | 네임스페이스 배열 |
| GET | `/api/opcua/browse` | 하위 노드 조회 (`nodeId` 없으면 Objects) |
| POST | `/api/opcua/read` | `{ "nodeIds": ["ns=2;s=..."] }` 값 읽기 |
| POST | `/api/opcua/write` | `{ "nodeId": "...", "value": ... }` 값 쓰기 |
| POST | `/api/opcua/subscribe` | 노드 구독 (이후 `GET /api/opcua/values`) |
| POST | `/api/opcua/unsubscribe` | 구독 해제 |
| GET | `/api/opcua/values` | 구독으로 수신한 최신 값 |
| GET | `/api/opcua/packages/{package_id}/nodes` | 산출물 `engineering.csv` 노드 목록 |
| GET | `/api/live/status` | 수집기 URL · REC 상태 |
| GET | `/api/live/collector` | 수집기 `/api/data` 프록시 (`url`, `seq`) |
| GET | `/api/live/record` | 기록 상태 `{"on","file","rows"}` |
| POST | `/api/live/record` | 기록 시작/정지 |
| GET | `/api/opcua/packages/{package_id}/mapping` | AAS Property ↔ 현장 NodeId 매핑 |
| GET | `/api/opcua/packages/{package_id}/live` | 구독 캐시의 매핑 값 · 그래프 프레임 |
| POST | `/api/opcua/packages/{package_id}/read` | 매핑된 AAS 속성을 현장 서버에서 읽기 (`?source=engineering` 이면 산출물 NodeId) |

`{aas_id}` 는 목록 API의 `shells[].idBase64`(base64url) 사용을 권장합니다. 슬래시가 없는 URN 원문도 허용합니다.

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
  repository.py    폴더 내 AASX 로드/업로드 (basyx AASXReader)
  opcua_catalog.py OPC-UA 서버 카탈로그 (endpoint 목록)
  opcua_export.py  OPC-UA nodeset/engineering/syscfg 생성
  opcua_mapping.py AAS Property ↔ 현장 NodeId 매핑
  opcua_api.py     OPC-UA 클라이언트 REST
  live_api.py      실시간 수집기 프록시 · REC
  live_frame.py    HDF7-9 형식 프레임 정규화
  live_record.py   CSV 기록
  opc_ua/          asyncua 클라이언트 래퍼 + 세션 (middleware 형식)
  serializers.py   간소화 트리 변환
  static/          플랫폼 홈 + HTML 트리 뷰어
scripts/
  create_sample_aasx.py  샘플 AASX 생성 (python -m scripts.create_sample_aasx)
```
