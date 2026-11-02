-- =====================================================================

-- 참고용: 운영 DB seongjang_testbed.manufacturing.opcua_sample 의 현재 구조 (읽기 전용 조회로 추출)

-- 실행용이 아닙니다. 애플리케이션은 이 파일을 실행하지 않으며, 테이블 변경은 DB 관리자가 합니다.

-- =====================================================================


CREATE TABLE manufacturing.opcua_sample (
    ts timestamp with time zone NOT NULL,
    node_pk integer NOT NULL,
    value_num double precision,
    value_bool boolean,
    value_text text,
    quality bigint NOT NULL DEFAULT 0,
    source_ts timestamp with time zone,
    received_at timestamp with time zone NOT NULL,
    CONSTRAINT opcua_sample_pkey PRIMARY KEY (node_pk, ts),
    CONSTRAINT opcua_sample_one_value CHECK ((num_nonnulls(value_num, value_bool, value_text) <= 1)),
    CONSTRAINT opcua_sample_quality_check CHECK (((quality >= 0) AND (quality <= '4294967295'::bigint))),
    CONSTRAINT opcua_sample_node_pk_fkey FOREIGN KEY (node_pk) REFERENCES manufacturing.equipment_node(node_pk)
);

CREATE INDEX opcua_sample_ts_idx ON manufacturing.opcua_sample USING btree (ts DESC);

-- TimescaleDB hypertable: 시간 컬럼 ts, 청크 간격 1일
-- SELECT create_hypertable('manufacturing.opcua_sample', 'ts', chunk_time_interval => INTERVAL '1 day');
