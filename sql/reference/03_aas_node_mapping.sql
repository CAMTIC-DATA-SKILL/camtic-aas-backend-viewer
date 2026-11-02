-- =====================================================================

-- 참고용: 운영 DB seongjang_testbed.manufacturing.aas_node_mapping 의 현재 구조 (읽기 전용 조회로 추출)

-- 실행용이 아닙니다. 애플리케이션은 이 파일을 실행하지 않으며, 테이블 변경은 DB 관리자가 합니다.

-- =====================================================================


CREATE TABLE manufacturing.aas_node_mapping (
    mapping_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    equipment_id text NOT NULL,
    node_id text NOT NULL,
    aas_submodel_id text NOT NULL,
    aas_id_short_path text NOT NULL,
    mapping_type text NOT NULL DEFAULT 'CURRENT_VALUE'::text,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT aas_node_mapping_pkey PRIMARY KEY (mapping_id),
    CONSTRAINT aas_node_mapping_mapping_type_check CHECK ((mapping_type = ANY (ARRAY['CURRENT_VALUE'::text, 'TIME_SERIES'::text, 'STATUS'::text]))),
    CONSTRAINT aas_node_mapping_equipment_id_node_id_fkey FOREIGN KEY (equipment_id, node_id) REFERENCES manufacturing.equipment_node(equipment_id, node_id) ON DELETE CASCADE,
    CONSTRAINT aas_node_mapping_equipment_property_key UNIQUE (equipment_id, aas_submodel_id, aas_id_short_path, mapping_type)
);

CREATE INDEX aas_node_mapping_node_idx ON manufacturing.aas_node_mapping USING btree (equipment_id, node_id);
CREATE INDEX aas_node_mapping_property_idx ON manufacturing.aas_node_mapping USING btree (aas_submodel_id, aas_id_short_path);
