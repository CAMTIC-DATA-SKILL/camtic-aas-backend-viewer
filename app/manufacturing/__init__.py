"""제조설비 시계열 데이터 (로컬 PostgreSQL + TimescaleDB, schema manufacturing).

AASX 템플릿 engineering.csv → equipment · equipment_node · aas_node_mapping.
미들웨어 값 → opcua_sample(원본) + equipment_latest(최신값) → /api/v1 조회.
"""
