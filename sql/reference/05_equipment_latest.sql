-- =====================================================================

-- 참고용: 운영 DB seongjang_testbed.manufacturing.equipment_latest 의 현재 구조 (읽기 전용 조회로 추출)

-- 실행용이 아닙니다. 애플리케이션은 이 파일을 실행하지 않으며, 테이블 변경은 DB 관리자가 합니다.

-- =====================================================================


CREATE TABLE manufacturing.equipment_latest (
    equipment_id text NOT NULL,
    node_id text NOT NULL,
    node_pk integer NOT NULL,
    last_ts timestamp with time zone NOT NULL,
    value_num double precision,
    value_bool boolean,
    value_text text,
    quality bigint NOT NULL,
    source_ts timestamp with time zone,
    received_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT equipment_latest_pkey PRIMARY KEY (equipment_id, node_id),
    CONSTRAINT equipment_latest_equipment_id_node_id_fkey FOREIGN KEY (equipment_id, node_id) REFERENCES manufacturing.equipment_node(equipment_id, node_id) ON DELETE CASCADE,
    CONSTRAINT equipment_latest_node_pk_fkey FOREIGN KEY (node_pk) REFERENCES manufacturing.equipment_node(node_pk),
    CONSTRAINT equipment_latest_node_pk_key UNIQUE (node_pk)
);
