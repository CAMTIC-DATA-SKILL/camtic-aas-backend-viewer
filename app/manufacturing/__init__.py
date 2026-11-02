"""제조설비 시계열 데이터 (로컬 PostgreSQL + TimescaleDB, schema manufacturing).

수집 시스템 → POST /api/v1/ingest → opcua_sample(원본) + equipment_latest(최신값) → /api/v1 조회.
"""
