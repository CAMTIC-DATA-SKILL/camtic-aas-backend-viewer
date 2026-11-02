-- 노드 이력 (DBeaver: :equipment_id, :node_id, :start, :end 입력. 예: '2026-11-02 10:00+09')

-- 1) 원시 데이터 (최대 10,000행)
SELECT s.ts, s.value_num, s.value_bool, s.value_text, s.quality, s.source_ts, s.received_at
FROM manufacturing.opcua_sample s
JOIN manufacturing.equipment_node n ON n.node_pk = s.node_pk
WHERE n.equipment_id = :equipment_id AND n.node_id = :node_id
  AND s.ts >= :start::timestamptz AND s.ts < :end::timestamptz
ORDER BY s.ts
LIMIT 10000;

-- 2) 다운샘플 (1초 구간 평균·최소·최대·마지막값·건수)
SELECT time_bucket(INTERVAL '1 second', s.ts) AS bucket,
       avg(s.value_num) AS avg, min(s.value_num) AS min, max(s.value_num) AS max,
       last(s.value_num, s.ts) AS last, count(*) AS n
FROM manufacturing.opcua_sample s
JOIN manufacturing.equipment_node n ON n.node_pk = s.node_pk
WHERE n.equipment_id = :equipment_id AND n.node_id = :node_id
  AND s.ts >= :start::timestamptz AND s.ts < :end::timestamptz
GROUP BY bucket
ORDER BY bucket;
