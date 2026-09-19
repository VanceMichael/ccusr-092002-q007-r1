-- 分段行程接续服务：路段图、队伍、签到、通告、评估、改线、决定与接驳占用。
-- 所有时间字段均为带时区偏移的 ISO 8601 字符串；标识符由提交方提供（*_ref）。

CREATE TABLE IF NOT EXISTS segments (
    segment_ref TEXT PRIMARY KEY,
    route_ref   TEXT NOT NULL,
    level       TEXT NOT NULL CHECK (level IN ('beginner', 'intermediate', 'advanced')),
    seq         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS exits (
    exit_ref  TEXT PRIMARY KEY,
    route_ref TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    conn_ref    TEXT PRIMARY KEY,
    from_ref    TEXT NOT NULL,
    to_ref      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('trail', 'cableway', 'evacuation')),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS segment_status (
    segment_ref TEXT PRIMARY KEY REFERENCES segments(segment_ref),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    team_ref   TEXT PRIMARY KEY,
    leader_ref TEXT NOT NULL,
    route_ref  TEXT NOT NULL,
    level      TEXT NOT NULL CHECK (level IN ('beginner', 'intermediate', 'advanced')),
    created_at TEXT NOT NULL
);

-- 游客名单：仅所属带队人员可读写，服务内不做跨队伍展示。
CREATE TABLE IF NOT EXISTS team_members (
    team_ref     TEXT NOT NULL REFERENCES teams(team_ref),
    member_ref   TEXT NOT NULL,
    display_name TEXT NOT NULL,
    note         TEXT,
    PRIMARY KEY (team_ref, member_ref)
);

-- 路段签到：occurred_at 为现场原始时间，uploaded_at 为服务端接收时间；
-- 断网补报时 occurred_at 明显早于 uploaded_at，source 记为 backfill。
CREATE TABLE IF NOT EXISTS checkins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_ref    TEXT NOT NULL REFERENCES teams(team_ref),
    segment_ref TEXT NOT NULL REFERENCES segments(segment_ref),
    event       TEXT NOT NULL CHECK (event IN ('arrive', 'leave')),
    occurred_at TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('live', 'backfill'))
);
CREATE INDEX IF NOT EXISTS idx_checkins_team ON checkins(team_ref, occurred_at);

-- 维护人员发布的封闭 / 疏散通道变化。
CREATE TABLE IF NOT EXISTS notices (
    notice_ref      TEXT PRIMARY KEY,
    target_kind     TEXT NOT NULL CHECK (target_kind IN ('segment', 'connection')),
    target_ref      TEXT NOT NULL,
    change          TEXT NOT NULL CHECK (change IN ('open', 'closed')),
    effective_from  TEXT NOT NULL,
    effective_until TEXT,
    published_by    TEXT NOT NULL,
    published_at    TEXT NOT NULL
);

-- 每次通告发布时对全部在场队伍的评估快照，供复盘使用。
CREATE TABLE IF NOT EXISTS evaluations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_ref      TEXT NOT NULL REFERENCES notices(notice_ref),
    team_ref        TEXT NOT NULL REFERENCES teams(team_ref),
    classification  TEXT NOT NULL CHECK (classification IN ('not_started', 'can_reroute', 'in_affected_zone')),
    position_segment TEXT,
    trapped_zone    TEXT NOT NULL,
    evaluated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluations_notice ON evaluations(notice_ref, team_ref);

-- 替代连接建议：只对仍能安全改线的队伍生成。
CREATE TABLE IF NOT EXISTS reroute_offers (
    offer_ref  TEXT PRIMARY KEY,
    team_ref   TEXT NOT NULL REFERENCES teams(team_ref),
    notice_ref TEXT NOT NULL REFERENCES notices(notice_ref),
    conn_ref   TEXT NOT NULL REFERENCES connections(conn_ref),
    status     TEXT NOT NULL DEFAULT 'offered' CHECK (status IN ('offered', 'accepted', 'declined', 'superseded')),
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_offers_team ON reroute_offers(team_ref, status);

-- 已入谷队伍的处置决定：等待 / 折返 / 人工护送。
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_ref    TEXT NOT NULL REFERENCES teams(team_ref),
    notice_ref  TEXT REFERENCES notices(notice_ref),
    decision    TEXT NOT NULL CHECK (decision IN ('wait', 'retreat', 'escort')),
    decided_by  TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_team ON decisions(team_ref, decided_at);

-- 出口与接驳车辆占用：同一出口或同一车辆的时间窗不得重叠。
CREATE TABLE IF NOT EXISTS bookings (
    booking_ref  TEXT PRIMARY KEY,
    team_ref     TEXT NOT NULL REFERENCES teams(team_ref),
    exit_ref     TEXT NOT NULL REFERENCES exits(exit_ref),
    vehicle_ref  TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released')),
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookings_resource ON bookings(exit_ref, vehicle_ref, status);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_segmented_itinerary');
