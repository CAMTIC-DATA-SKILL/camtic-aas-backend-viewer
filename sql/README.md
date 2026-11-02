# sql/

운영 TimescaleDB(`seongjang_testbed`, 스키마 `manufacturing`)와 관련된 SQL을 따로 모아 둔 폴더입니다.
**애플리케이션은 이 폴더의 SQL로 테이블을 만들거나 바꾸지 않습니다.** 테이블 변경은 DB 관리자가 합니다.

| 폴더 | 용도 |
|---|---|
| `reference/` | 운영 DB의 현재 테이블 구조 (참고용, 실행 금지) |
| `inspection/` | 읽기 전용 점검 쿼리 (DBeaver에서 실행) |
| `queries/` | 조회 API가 쓰는 SELECT 쿼리 원본 |

## reference/ — 테이블 구조

운영 DB 카탈로그를 읽기 전용으로 조회해 추출한 구조입니다. 실제 DB와 다르면 운영 DB가 기준입니다.

| 파일 | 테이블 | 역할 |
|---|---|---|
| `01_equipment.sql` | `equipment` | 설비 정보 (`data_source`는 `REAL`만 사용) |
| `02_equipment_node.sql` | `equipment_node` | 설비별 OPC-UA 노드, `node_pk`는 자동 생성, `enabled`로 수집 여부 결정 |
| `03_aas_node_mapping.sql` | `aas_node_mapping` | 노드와 AAS Property 연결 |
| `04_opcua_sample.sql` | `opcua_sample` | 시계열 값 (TimescaleDB hypertable, 1일 청크) |
| `05_equipment_latest.sql` | `equipment_latest` | 노드별 최신값 |
| `06_alarm_event.sql` | `alarm_event` | 알람 발생·복구 이력 |

관계: `equipment` → `equipment_node` → (`aas_node_mapping`, `opcua_sample`, `equipment_latest`, `alarm_event`)

## 앱이 의존하는 제약

- `opcua_sample (node_pk, ts)` 기본키: 중복 저장은 `ON CONFLICT DO NOTHING`으로 무시
- `equipment_latest (equipment_id, node_id)`: 최신값 UPSERT 기준
- `alarm_event_open_uniq`: 같은 설비·노드·종류의 진행 중 알람은 하나만 허용
- `alarm_event`: `status = 'CLEARED'` 일 때만 `ended_at`이 채워짐

## 현재 상태 (2026-11-02 기준)

- 압축과 보존 정책이 설정되어 있지 않습니다.
- 수집 시작 전이라 `opcua_sample`, `equipment_latest`는 비어 있습니다.
