-- 설비의 노드별 최신값 (DBeaver: :equipment_id 에 값 입력)
SELECT n.node_id, n.node_name, n.data_type, n.unit,
       l.last_ts, l.value_num, l.value_bool, l.value_text, l.quality,
       CASE WHEN l.quality IS NULL THEN 'NO_DATA'
            WHEN l.quality >= 2147483648 THEN 'BAD'        -- 0x80000000
            WHEN l.quality >= 1073741824 THEN 'UNCERTAIN'  -- 0x40000000
            ELSE 'GOOD' END AS quality_class,
       l.received_at, now() - l.received_at AS age
FROM manufacturing.equipment_node n
LEFT JOIN manufacturing.equipment_latest l
       ON l.equipment_id = n.equipment_id AND l.node_id = n.node_id
WHERE n.equipment_id = :equipment_id
ORDER BY n.node_name;
