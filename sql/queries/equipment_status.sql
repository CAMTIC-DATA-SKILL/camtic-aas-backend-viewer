-- 설비 수집 상태: 마지막 수신, 최근 60초 수집 건수(기대치 대비), 끊긴 노드 수, 열린 알람 수
WITH nodes AS (
    SELECT n.equipment_id, n.node_pk, n.sampling_ms, l.received_at, l.last_ts
    FROM manufacturing.equipment_node n
    LEFT JOIN manufacturing.equipment_latest l ON l.node_pk = n.node_pk
    WHERE n.enabled
),
recent AS (
    SELECT n.equipment_id, count(*) AS samples_60s
    FROM manufacturing.opcua_sample s
    JOIN manufacturing.equipment_node n ON n.node_pk = s.node_pk
    WHERE s.ts >= now() - INTERVAL '60 seconds'
    GROUP BY n.equipment_id
)
SELECT e.equipment_id, e.equipment_type, e.equipment_name,
       count(nd.node_pk)                                             AS enabled_nodes,
       max(nd.received_at)                                           AS last_received_at,
       coalesce(r.samples_60s, 0)                                    AS samples_60s,
       coalesce(sum(60000 / nd.sampling_ms), 0)::bigint              AS expected_60s,
       count(*) FILTER (WHERE nd.received_at IS NULL
                           OR nd.received_at < now() - make_interval(secs => nd.sampling_ms * 50 / 1000.0))
                                                                     AS stale_nodes,
       (SELECT count(*) FROM manufacturing.alarm_event a
         WHERE a.equipment_id = e.equipment_id AND a.status <> 'CLEARED') AS open_alarms
FROM manufacturing.equipment e
LEFT JOIN nodes nd ON nd.equipment_id = e.equipment_id
LEFT JOIN recent r ON r.equipment_id = e.equipment_id
GROUP BY e.equipment_id, e.equipment_type, e.equipment_name, r.samples_60s
ORDER BY e.equipment_id;
