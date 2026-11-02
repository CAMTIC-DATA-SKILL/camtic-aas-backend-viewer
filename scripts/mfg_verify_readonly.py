"""Exercise query APIs against .env DB with server-enforced read-only connections.

No application lifespan, OPC-UA connection, background task or ingest request is run.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from app.manufacturing.api import ManufacturingRuntime, router
from app.manufacturing.db import create_sessionmaker, database_url
from app.manufacturing.ingest import NodeCache
from app.manufacturing.models import Base
from config import load_manufacturing_config
from sqlalchemy import text


async def verify():
    cfg = load_manufacturing_config()
    engine = create_async_engine(database_url(cfg), connect_args={
        'server_settings': {'default_transaction_read_only': 'on', 'statement_timeout': '10000'},
        'timeout': 5,
    })
    app = FastAPI()
    app.state.manufacturing = ManufacturingRuntime(cfg, engine, create_sessionmaker(engine), NodeCache())
    app.include_router(router)
    result = {'target': {'host': cfg.db_host, 'port': cfg.db_port, 'database': cfg.db_name}}
    try:
        async with engine.connect() as conn:
            result['transaction_read_only'] = await conn.scalar(text('SHOW transaction_read_only'))
            actual = (await conn.execute(text('''
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = 'manufacturing'
            '''))).all()
            columns = {}
            for table, column in actual:
                columns.setdefault(table, set()).add(column)
            result['missing_model_columns'] = {
                t.name: sorted(set(t.columns.keys()) - columns.get(t.name, set()))
                for t in Base.metadata.sorted_tables
            }
        headers = {'X-API-Key': cfg.read_api_key} if cfg.read_api_key else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://readonly', headers=headers) as client:
            paths = ['/summary', '/equipment', '/nodes', '/latest', '/recent', '/mappings', '/alarms', '/db-status']
            result['http'] = {}
            for path in paths:
                response = await client.get('/api/v1/timeseries' + path)
                result['http'][path] = response.status_code
            nodes = (await client.get('/api/v1/timeseries/nodes', params={'equipment_id': cfg.equipment_id})).json()
            if nodes:
                params = {'equipment_id': cfg.equipment_id, 'node_id': nodes[0]['node_id'],
                          'start': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
                for mode in ['raw', 'bucket']:
                    r = await client.get('/api/v1/timeseries/history', params={**params, 'mode': mode})
                    result['http']['/history:' + mode] = r.status_code
                for path in ['/history.csv', '/node-status']:
                    r = await client.get('/api/v1/timeseries' + path, params=params)
                    result['http'][path] = r.status_code
                r = await client.get('/api/v1/timeseries/stream', params={'equipment_id': cfg.equipment_id, 'max_events': 1})
                result['http']['/stream'] = r.status_code
                result['sse_db_connected'] = '"db_connected": true' in r.text
            result['simulator_routes'] = [p for p in app.openapi()['paths'] if 'simulator' in p]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == '__main__':
    try:
        asyncio.run(verify())
    except Exception as exc:
        print(json.dumps({'verified': False, 'error_type': type(exc).__name__}))
        raise SystemExit(1)
