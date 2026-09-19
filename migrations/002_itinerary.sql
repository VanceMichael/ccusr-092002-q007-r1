
-- 分段行程接续服务
-- 所有时间列同时保存原始字符串与 UTC 归一化值：
-- *_raw 保留带队者现场记录的原始时间，UTC 列用于排序和时间窗比较。

CREATE TABLE leaders (
    leader_ref TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    contact TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE teams (
    team_ref TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    leader_ref TEXT NOT NULL REFERENCES leaders(leader_ref),
    trail_level TEXT NOT NULL CHECK (trail_level IN ('BEGINNER', 'INTERMEDIATE', 'ADVANCED')),
    created_at TEXT NOT NULL
);

-- 游客名单只对所属带队者可见，应用层强制隔离。
CREATE TABLE visitors (
    team_ref TEXT NOT NULL REFERENCES teams(team_ref),
    visitor_ref TEXT NOT NULL,
    display_name TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (team_ref, visitor_ref)
);

CREATE TABLE segments (
    segment_ref TEXT PRIMARY KEY,
    route_ref TEXT NOT NULL,
    display_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    in_valley INTEGER NOT NULL DEFAULT 0,
    capacity INTEGER NOT NULL
);

CREATE TABLE exits (
    exit_ref TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);

CREATE TABLE shuttles (
    shuttle_ref TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    capacity INTEGER NOT NULL
);

CREATE TABLE team_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_ref TEXT NOT NULL REFERENCES teams(team_ref),
    segment_ref TEXT NOT NULL REFERENCES segments(segment_ref),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL
);

-- 到/离段签到；断网补报时 occurred_at 是现场原始时间，uploaded_at 是实际入库时间。
CREATE TABLE checkin_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_ref TEXT NOT NULL REFERENCES teams(team_ref),
    segment_ref TEXT NOT NULL REFERENCES segments(segment_ref),
    action TEXT NOT NULL CHECK (action IN ('ENTER', 'LEAVE')),
    occurred_at_raw TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    is_backfill INTEGER NOT NULL DEFAULT 0,
    recorded_by TEXT NOT NULL
);
CREATE INDEX idx_checkin_team_time ON checkin_events (team_ref, occurred_at, id);

-- 索道等通行设施状态，按设施只取最新一条生效。
CREATE TABLE ropeway_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'SUSPENDED')),
    changed_at TEXT NOT NULL,
    notice TEXT,
    changed_by TEXT NOT NULL
);
CREATE INDEX idx_ropeway_facility ON ropeway_status (facility_ref, id DESC);

-- 路段封闭与解封。
CREATE TABLE closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_ref TEXT NOT NULL REFERENCES segments(segment_ref),
    reason TEXT,
    opened_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    lifted_at TEXT,
    published_by TEXT NOT NULL
);

-- 维护人员发布的疏散通道。
CREATE TABLE evacuation_channels (
    channel_ref TEXT PRIMARY KEY,
    from_segment_ref TEXT NOT NULL REFERENCES segments(segment_ref),
    direction TEXT NOT NULL,
    to_exit_ref TEXT REFERENCES exits(exit_ref),
    open INTEGER NOT NULL DEFAULT 1,
    published_at TEXT NOT NULL,
    published_by TEXT NOT NULL
);

-- 仅对未入谷队伍发布的替代连接。
CREATE TABLE alternative_connections (
    connection_ref TEXT PRIMARY KEY,
    from_segment_ref TEXT NOT NULL REFERENCES segments(segment_ref),
    to_segment_ref TEXT NOT NULL REFERENCES segments(segment_ref),
    kind TEXT NOT NULL,
    trail_level TEXT CHECK (trail_level IS NULL OR trail_level IN ('BEGINNER', 'INTERMEDIATE', 'ADVANCED')),
    active INTEGER NOT NULL DEFAULT 1,
    published_at TEXT NOT NULL,
    published_by TEXT NOT NULL,
    note TEXT
);

-- 未入谷队伍的改线决定，一队一条。
CREATE TABLE reroute_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_ref TEXT NOT NULL UNIQUE REFERENCES teams(team_ref),
    connection_ref TEXT NOT NULL REFERENCES alternative_connections(connection_ref),
    decided_at TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    note TEXT
);

-- 已入谷队伍的处置决定：等待、折返、人工护送；允许更新并保留被取代的历史。
CREATE TABLE valley_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_ref TEXT NOT NULL REFERENCES teams(team_ref),
    decision TEXT NOT NULL CHECK (decision IN ('WAIT', 'RETURN', 'ESCORT')),
    decided_at TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    note TEXT,
    superseded INTEGER NOT NULL DEFAULT 0
);

-- 出口与接驳车辆的时间窗占用，互斥规则在服务层按半开区间重叠判定。
CREATE TABLE reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_ref TEXT NOT NULL REFERENCES teams(team_ref),
    exit_ref TEXT REFERENCES exits(exit_ref),
    shuttle_ref TEXT REFERENCES shuttles(shuttle_ref),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CLAIMED' CHECK (status IN ('CLAIMED', 'RELEASED')),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    CHECK (exit_ref IS NOT NULL OR shuttle_ref IS NOT NULL)
);
CREATE INDEX idx_reservations_windows ON reservations (status, exit_ref, shuttle_ref);

-- 复盘结论：每支队伍最终如何离开受影响区域。
CREATE TABLE evacuation_outcomes (
    team_ref TEXT PRIMARY KEY REFERENCES teams(team_ref),
    how TEXT NOT NULL CHECK (how IN ('REROUTED', 'WAITED_AND_RESUMED', 'TURNED_BACK', 'ESCORTED')),
    exit_ref TEXT REFERENCES exits(exit_ref),
    shuttle_ref TEXT REFERENCES shuttles(shuttle_ref),
    left_affected_at_raw TEXT NOT NULL,
    left_affected_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    note TEXT
);
