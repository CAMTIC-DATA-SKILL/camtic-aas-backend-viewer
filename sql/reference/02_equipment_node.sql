-- =====================================================================

-- 참고용: 운영 DB seongjang_testbed.manufacturing.equipment_node 의 현재 구조 (읽기 전용 조회로 추출)

-- 실행용이 아닙니다. 애플리케이션은 이 파일을 실행하지 않으며, 테이블 변경은 DB 관리자가 합니다.

-- =====================================================================


CREATE TABLE manufacturing.equipment_node (
    equipment_id text NOT NULL,
    node_id text NOT NULL,
    node_pk integer GENERATED ALWAYS AS IDENTITY NOT NULL,
    node_name text NOT NULL,
    data_type text NOT NULL,
    unit text,
    sampling_ms integer NOT NULL DEFAULT 100,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT equipment_node_pkey PRIMARY KEY (equipment_id, node_id),
    CONSTRAINT equipment_node_data_type_check CHECK ((data_type = ANY (ARRAY['Boolean'::text, 'SByte'::text, 'Byte'::text, 'Int16'::text, 'UInt16'::text, 'Int32'::text, 'UInt32'::text, 'Int64'::text, 'UInt64'::text, 'Float'::text, 'Double'::text, 'String'::text, 'DateTime'::text]))),
    CONSTRAINT equipment_node_sampling_ms_check CHECK ((sampling_ms > 0)),
    CONSTRAINT equipment_node_equipment_id_fkey FOREIGN KEY (equipment_id) REFERENCES manufacturing.equipment(equipment_id) ON UPDATE CASCADE,
    CONSTRAINT equipment_node_node_pk_key UNIQUE (node_pk)
);
