"""Read-only inspection of the existing manufacturing database; never starts the app."""
import asyncio
import json
import re

import asyncpg

from app.manufacturing.models import Base
from app.opcua_catalog import load_catalog
from config import load_manufacturing_config

TABLES = ['equipment', 'equipment_node', 'aas_node_mapping', 'opcua_sample', 'equipment_latest', 'alarm_event']


def _unique_columns(index_def: str) -> set[str]:
    columns = re.search(r'\(([^)]*)\)', index_def.split('USING', 1)[-1])
    return {c.strip().strip('"') for c in columns.group(1).split(',')} if columns else set()


def _has_unique(indexes: list[dict], table: str, columns: set[str]) -> bool:
    return any(
        i['tablename'] == table and 'UNIQUE' in i['indexdef'].upper() and _unique_columns(i['indexdef']) == columns
        for i in indexes
    )


def preflight(result: dict, equipment) -> dict:
    """실제 수집을 켜기 전에 앱이 기대하는 DB 조건을 판정한다 (true 여야 통과)."""
    indexes = result['indexes']
    db_columns: dict[str, set[str]] = {}
    for row in result['columns']:
        db_columns.setdefault(row['table_name'], set()).add(row['column_name'])
    model_missing = {
        t.name: sorted(set(t.columns.keys()) - db_columns.get(t.name, set()))
        for t in Base.metadata.sorted_tables
    }
    server = load_catalog().default_server()
    field = result['field']
    checks = {
        # INSERT ... ON CONFLICT 가 동작하려면 같은 열 조합의 유니크 인덱스가 있어야 한다.
        'opcua_sample_unique_node_pk_ts': _has_unique(indexes, 'opcua_sample', {'node_pk', 'ts'}),
        'equipment_latest_unique_equipment_id_node_id': _has_unique(indexes, 'equipment_latest', {'equipment_id', 'node_id'}),
        'equipment_node_unique_equipment_id_node_id': _has_unique(indexes, 'equipment_node', {'equipment_id', 'node_id'}),
        'equipment_node_unique_node_pk': _has_unique(indexes, 'equipment_node', {'node_pk'}),
        # 알람 자동 기록(MFG_ALARMS_ENABLED=true)에만 필요한 조건
        'alarm_event_open_unique_partial_index': any(
            i['tablename'] == 'alarm_event' and 'UNIQUE' in i['indexdef'].upper()
            and 'alarm_type' in i['indexdef'] and 'CLEARED' in i['indexdef'] for i in indexes),
        'opcua_sample_is_hypertable': bool(result['hypertable']),
        'model_columns_all_exist_in_db': not any(model_missing.values()),
        'field_equipment_exists': equipment is not None,
        'field_equipment_is_real': bool(equipment and equipment['data_source'] == 'REAL'),
        'field_equipment_enabled': bool(equipment and equipment['enabled']),
        # 기록기는 equipment.opcua_endpoint 가 현재 접속 endpoint 와 문자열로 같을 때만 노드를 고른다.
        'field_endpoint_matches_catalog': bool(equipment and server and equipment['opcua_endpoint'] == server.endpoint),
        'field_nodes_23_active_18_inactive_5_unmapped_0': (
            (field['total'], field['enabled'], field['total'] - field['enabled'], field['unmapped']) == (23, 18, 5, 0)),
    }
    return {
        'all_pass': all(checks.values()),
        'checks': checks,
        'model_columns_missing_in_db': model_missing,
        'catalog_endpoint': server.endpoint if server else None,
    }


async def inspect():
    cfg = load_manufacturing_config()
    conn = await asyncpg.connect(
        host=cfg.db_host, port=cfg.db_port, database=cfg.db_name,
        user=cfg.db_user, password=cfg.db_password, timeout=5,
        server_settings={'default_transaction_read_only': 'on', 'statement_timeout': '10000'},
    )
    try:
        async with conn.transaction(readonly=True):
            result = {}
            result['target'] = {'host': cfg.db_host, 'port': cfg.db_port, 'database': cfg.db_name}
            result['columns'] = [dict(r) for r in await conn.fetch('''
                SELECT table_name, column_name, data_type, is_nullable, column_default, is_identity
                FROM information_schema.columns
                WHERE table_schema = 'manufacturing' AND table_name = ANY($1::text[])
                ORDER BY table_name, ordinal_position
            ''', TABLES)]
            result['constraints'] = [dict(r) for r in await conn.fetch('''
                SELECT c.relname AS table_name, k.conname, pg_get_constraintdef(k.oid) AS definition
                FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'manufacturing' AND c.relname = ANY($1::text[])
            ''', TABLES)]
            result['indexes'] = [dict(r) for r in await conn.fetch('''
                SELECT tablename, indexname, indexdef FROM pg_indexes
                WHERE schemaname = 'manufacturing' AND tablename = ANY($1::text[])
            ''', TABLES)]
            result['hypertable'] = [dict(r) for r in await conn.fetch('''
                SELECT * FROM timescaledb_information.dimensions
                WHERE hypertable_schema = 'manufacturing' AND hypertable_name = 'opcua_sample'
            ''')]
            result['field'] = dict(await conn.fetchrow('''
                SELECT count(*) AS total, count(*) FILTER (WHERE enabled) AS enabled,
                    (SELECT count(*) FROM manufacturing.aas_node_mapping WHERE equipment_id = $1) AS mappings,
                    count(*) FILTER (WHERE NOT EXISTS (
                        SELECT 1 FROM manufacturing.aas_node_mapping m
                        WHERE m.equipment_id = n.equipment_id AND m.node_id = n.node_id)) AS unmapped
                FROM manufacturing.equipment_node n WHERE equipment_id = $1
            ''', cfg.equipment_id))
            equipment = await conn.fetchrow('''
                SELECT equipment_id, data_source, enabled, opcua_endpoint
                FROM manufacturing.equipment WHERE equipment_id = $1
            ''', cfg.equipment_id)
            result['field_equipment'] = dict(equipment) if equipment else None
            result['preflight'] = preflight(result, equipment)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        await conn.close()


if __name__ == '__main__':
    try:
        asyncio.run(inspect())
    except Exception as exc:
        # Never print connection arguments or database exception text containing credentials.
        print(json.dumps({'connected': False, 'error_type': type(exc).__name__}))
        raise SystemExit(1)
