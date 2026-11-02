-- =====================================================================

-- 참고용: 운영 DB seongjang_testbed.manufacturing.equipment 의 현재 구조 (읽기 전용 조회로 추출)

-- 실행용이 아닙니다. 애플리케이션은 이 파일을 실행하지 않으며, 테이블 변경은 DB 관리자가 합니다.

-- =====================================================================


CREATE TABLE manufacturing.equipment (
    equipment_id text NOT NULL,
    equipment_type text NOT NULL,
    equipment_name text NOT NULL,
    manufacturer text,
    model_name text,
    serial_number text,
    aas_id text,
    opcua_endpoint text,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    data_source text NOT NULL DEFAULT 'REAL'::text,
    CONSTRAINT equipment_pkey PRIMARY KEY (equipment_id),
    CONSTRAINT equipment_data_source_check CHECK ((data_source = ANY (ARRAY['REAL'::text, 'SIMULATION'::text]))),
    CONSTRAINT equipment_equipment_id_check CHECK ((equipment_id ~ '^[A-Za-z0-9_.-]{1,64}$'::text)),
    CONSTRAINT equipment_equipment_type_check CHECK ((equipment_type = ANY (ARRAY['ROBOT'::text, 'PLC'::text, 'AMR'::text, 'SENSOR'::text, 'MACHINE'::text])))
);

CREATE INDEX equipment_aas_id_idx ON manufacturing.equipment USING btree (aas_id);
