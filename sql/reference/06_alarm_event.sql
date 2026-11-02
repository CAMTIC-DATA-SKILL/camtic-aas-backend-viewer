-- =====================================================================

-- 참고용: 운영 DB seongjang_testbed.manufacturing.alarm_event 의 현재 구조 (읽기 전용 조회로 추출)

-- 실행용이 아닙니다. 애플리케이션은 이 파일을 실행하지 않으며, 테이블 변경은 DB 관리자가 합니다.

-- =====================================================================


CREATE TABLE manufacturing.alarm_event (
    alarm_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    equipment_id text NOT NULL,
    node_id text,
    alarm_type text NOT NULL,
    severity text NOT NULL,
    message text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    ended_at timestamp with time zone,
    status text NOT NULL DEFAULT 'ACTIVE'::text,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT alarm_event_pkey PRIMARY KEY (alarm_id),
    CONSTRAINT alarm_event_alarm_type_check CHECK ((alarm_type = ANY (ARRAY['THRESHOLD'::text, 'COMM_LOSS'::text, 'BAD_QUALITY'::text, 'UNCERTAIN_QUALITY'::text, 'STALE'::text, 'OTHER'::text]))),
    CONSTRAINT alarm_event_cleared_check CHECK (((status = 'CLEARED'::text) = (ended_at IS NOT NULL))),
    CONSTRAINT alarm_event_severity_check CHECK ((severity = ANY (ARRAY['INFO'::text, 'WARNING'::text, 'CRITICAL'::text]))),
    CONSTRAINT alarm_event_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'ACKED'::text, 'CLEARED'::text]))),
    CONSTRAINT alarm_event_time_check CHECK (((ended_at IS NULL) OR (ended_at >= started_at))),
    CONSTRAINT alarm_event_equipment_id_fkey FOREIGN KEY (equipment_id) REFERENCES manufacturing.equipment(equipment_id),
    CONSTRAINT alarm_event_equipment_id_node_id_fkey FOREIGN KEY (equipment_id, node_id) REFERENCES manufacturing.equipment_node(equipment_id, node_id)
);

CREATE INDEX alarm_event_equipment_started_idx ON manufacturing.alarm_event USING btree (equipment_id, started_at DESC);
CREATE UNIQUE INDEX alarm_event_open_uniq ON manufacturing.alarm_event USING btree (equipment_id, COALESCE(node_id, ''::text), alarm_type) WHERE (status <> 'CLEARED'::text);
CREATE INDEX alarm_event_recent_idx ON manufacturing.alarm_event USING btree (started_at DESC);
