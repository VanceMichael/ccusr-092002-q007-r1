"""分段行程接续的领域规则。

- 带队人员登记路段实际到达 / 离开时间；断网补报保留原始时间与上传时间。
- 维护人员发布封闭与疏散通道变化，系统重新评估全部在场队伍。
- 仍能安全改线的队伍获得替代连接建议；已入谷（受困）队伍只登记
  等待 / 折返 / 人工护送决定。
- 出口与接驳车辆按时间窗互斥占用。
- 复盘时按队伍给出最终离开受影响区域的方式。
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

LEVELS = ("beginner", "intermediate", "advanced")
CONNECTION_KINDS = ("trail", "cableway", "evacuation")
DECISIONS = ("wait", "retreat", "escort")
BACKFILL_THRESHOLD = timedelta(minutes=15)


class DomainError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class BadRequest(DomainError):
    status = 400
    code = "bad_request"


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Conflict(DomainError):
    status = 409
    code = "conflict"


# ---------------------------------------------------------------- 时间工具

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value, field: str) -> datetime:
    if not isinstance(value, str):
        raise BadRequest(f"{field} 必须是带时区偏移的 ISO 8601 字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise BadRequest(f"{field} 不是合法的 ISO 8601 时间：{value}")
    if parsed.tzinfo is None:
        raise BadRequest(f"{field} 必须包含时区偏移：{value}")
    return parsed


def _require_fields(payload: dict, fields) -> None:
    missing = [name for name in fields if payload.get(name) in (None, "")]
    if missing:
        raise BadRequest("缺少字段：" + ", ".join(missing))


def _new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------- 登记

def upsert_segment(conn, segment_ref: str, payload: dict) -> dict:
    _require_fields(payload, ("route_ref", "level", "seq"))
    if payload["level"] not in LEVELS:
        raise BadRequest(f"level 必须是 {LEVELS} 之一")
    conn.execute(
        "INSERT INTO segments(segment_ref, route_ref, level, seq) VALUES (?,?,?,?) "
        "ON CONFLICT(segment_ref) DO UPDATE SET route_ref=excluded.route_ref, "
        "level=excluded.level, seq=excluded.seq",
        (segment_ref, payload["route_ref"], payload["level"], int(payload["seq"])),
    )
    conn.execute(
        "INSERT OR IGNORE INTO segment_status(segment_ref, status) VALUES (?, 'open')",
        (segment_ref,),
    )
    return {"segment_ref": segment_ref, "route_ref": payload["route_ref"],
            "level": payload["level"], "seq": int(payload["seq"])}


def upsert_exit(conn, exit_ref: str, payload: dict) -> dict:
    _require_fields(payload, ("route_ref",))
    conn.execute(
        "INSERT INTO exits(exit_ref, route_ref) VALUES (?,?) "
        "ON CONFLICT(exit_ref) DO UPDATE SET route_ref=excluded.route_ref",
        (exit_ref, payload["route_ref"]),
    )
    return {"exit_ref": exit_ref, "route_ref": payload["route_ref"]}


def upsert_connection(conn, conn_ref: str, payload: dict) -> dict:
    _require_fields(payload, ("from_ref", "to_ref", "kind"))
    if payload["kind"] not in CONNECTION_KINDS:
        raise BadRequest(f"kind 必须是 {CONNECTION_KINDS} 之一")
    for endpoint in (payload["from_ref"], payload["to_ref"]):
        _require_endpoint(conn, endpoint)
    conn.execute(
        "INSERT INTO connections(conn_ref, from_ref, to_ref, kind, status) "
        "VALUES (?,?,?,?,'open') "
        "ON CONFLICT(conn_ref) DO UPDATE SET from_ref=excluded.from_ref, "
        "to_ref=excluded.to_ref, kind=excluded.kind",
        (conn_ref, payload["from_ref"], payload["to_ref"], payload["kind"]),
    )
    return {"conn_ref": conn_ref, "from_ref": payload["from_ref"],
            "to_ref": payload["to_ref"], "kind": payload["kind"]}


def _require_endpoint(conn, ref: str) -> None:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM segments WHERE segment_ref=?) + "
        "(SELECT COUNT(*) FROM exits WHERE exit_ref=?) AS n",
        (ref, ref),
    ).fetchone()
    if row["n"] == 0:
        raise NotFound(f"连接端点不存在：{ref}")


def register_team(conn, payload: dict) -> dict:
    _require_fields(payload, ("team_ref", "leader_ref", "route_ref", "level"))
    if payload["level"] not in LEVELS:
        raise BadRequest(f"level 必须是 {LEVELS} 之一")
    try:
        conn.execute(
            "INSERT INTO teams(team_ref, leader_ref, route_ref, level, created_at) "
            "VALUES (?,?,?,?,?)",
            (payload["team_ref"], payload["leader_ref"], payload["route_ref"],
             payload["level"], now_iso()),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise Conflict(f"队伍已登记：{payload['team_ref']}")
        raise
    return {"team_ref": payload["team_ref"], "leader_ref": payload["leader_ref"],
            "route_ref": payload["route_ref"], "level": payload["level"]}


def _get_team(conn, team_ref: str):
    row = conn.execute("SELECT * FROM teams WHERE team_ref=?", (team_ref,)).fetchone()
    if row is None:
        raise NotFound(f"队伍不存在：{team_ref}")
    return row


def _require_leader(team, leader_ref) -> None:
    if not leader_ref or leader_ref != team["leader_ref"]:
        raise Forbidden("仅所属带队人员可以访问该队伍的游客名单")


# ---------------------------------------------------------------- 游客名单

def replace_members(conn, team_ref: str, leader_ref, payload: dict) -> dict:
    team = _get_team(conn, team_ref)
    _require_leader(team, leader_ref)
    members = payload.get("members")
    if not isinstance(members, list):
        raise BadRequest("members 必须是数组")
    conn.execute("DELETE FROM team_members WHERE team_ref=?", (team_ref,))
    stored = []
    for item in members:
        _require_fields(item, ("member_ref", "display_name"))
        conn.execute(
            "INSERT INTO team_members(team_ref, member_ref, display_name, note) "
            "VALUES (?,?,?,?)",
            (team_ref, item["member_ref"], item["display_name"], item.get("note")),
        )
        stored.append({"member_ref": item["member_ref"],
                       "display_name": item["display_name"], "note": item.get("note")})
    return {"team_ref": team_ref, "members": stored}


def list_members(conn, team_ref: str, leader_ref) -> dict:
    team = _get_team(conn, team_ref)
    _require_leader(team, leader_ref)
    rows = conn.execute(
        "SELECT member_ref, display_name, note FROM team_members "
        "WHERE team_ref=? ORDER BY member_ref",
        (team_ref,),
    ).fetchall()
    return {"team_ref": team_ref, "members": [dict(row) for row in rows]}


# ---------------------------------------------------------------- 签到与占用

def record_checkin(conn, team_ref: str, payload: dict) -> dict:
    _get_team(conn, team_ref)
    _require_fields(payload, ("segment_ref", "event", "occurred_at"))
    segment = conn.execute(
        "SELECT segment_ref FROM segments WHERE segment_ref=?", (payload["segment_ref"],)
    ).fetchone()
    if segment is None:
        raise NotFound(f"路段不存在：{payload['segment_ref']}")
    if payload["event"] not in ("arrive", "leave"):
        raise BadRequest("event 必须是 arrive 或 leave")
    occurred = parse_time(payload["occurred_at"], "occurred_at")
    uploaded = datetime.now(timezone.utc)
    source = "backfill" if uploaded - occurred > BACKFILL_THRESHOLD else "live"
    cursor = conn.execute(
        "INSERT INTO checkins(team_ref, segment_ref, event, occurred_at, uploaded_at, source) "
        "VALUES (?,?,?,?,?,?)",
        (team_ref, payload["segment_ref"], payload["event"],
         occurred.isoformat(timespec="seconds"), uploaded.isoformat(timespec="seconds"), source),
    )
    return {"id": cursor.lastrowid, "team_ref": team_ref,
            "segment_ref": payload["segment_ref"], "event": payload["event"],
            "occurred_at": occurred.isoformat(timespec="seconds"),
            "uploaded_at": uploaded.isoformat(timespec="seconds"), "source": source}


def list_checkins(conn, team_ref: str) -> dict:
    _get_team(conn, team_ref)
    rows = conn.execute(
        "SELECT segment_ref, event, occurred_at, uploaded_at, source FROM checkins "
        "WHERE team_ref=? ORDER BY occurred_at, id",
        (team_ref,),
    ).fetchall()
    return {"team_ref": team_ref, "checkins": [dict(row) for row in rows]}


def _latest_event(conn, team_ref: str):
    return conn.execute(
        "SELECT segment_ref, event FROM checkins WHERE team_ref=? "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (team_ref,),
    ).fetchone()


def team_position(conn, team_ref: str):
    """队伍最后已知位置：最近一条签到（按现场时间）所在的路段。"""
    row = _latest_event(conn, team_ref)
    return row["segment_ref"] if row else None


def occupancy(conn) -> dict:
    """路线占用：当前停留在各路段的队伍（最后一条签到为到达）。"""
    segments = {}
    teams = conn.execute("SELECT team_ref FROM teams").fetchall()
    for team in teams:
        latest = _latest_event(conn, team["team_ref"])
        if latest and latest["event"] == "arrive":
            segments.setdefault(latest["segment_ref"], []).append(team["team_ref"])
    return {"segments": [{"segment_ref": ref, "teams": sorted(names)}
                         for ref, names in sorted(segments.items())]}


# ---------------------------------------------------------------- 图与受困区域

def _open_graph(conn):
    """返回 (连通分量列表, 出口集合, 已封闭路段集合)。边为开放连接，节点为开放路段与出口。"""
    closed_segments = {
        row["segment_ref"]
        for row in conn.execute("SELECT segment_ref FROM segment_status WHERE status='closed'")
    }
    open_segments = {
        row["segment_ref"] for row in conn.execute("SELECT segment_ref FROM segments")
    } - closed_segments
    exits = {row["exit_ref"] for row in conn.execute("SELECT exit_ref FROM exits")}
    adjacency = {node: set() for node in open_segments | exits}
    for row in conn.execute(
        "SELECT from_ref, to_ref FROM connections WHERE status='open'"
    ):
        a, b = row["from_ref"], row["to_ref"]
        if a in adjacency and b in adjacency:
            adjacency[a].add(b)
            adjacency[b].add(a)
    components = []
    seen = set()
    for node in adjacency:
        if node in seen:
            continue
        stack, component = [node], set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency[current] - component)
        seen |= component
        components.append(component)
    return components, exits, closed_segments


def _trapped_zone(components, exits) -> set:
    """受困区域：不含任何出口的连通分量（封闭后无法自行离场的路段集合）。"""
    trapped = set()
    for component in components:
        if not component & exits:
            trapped |= component
    return trapped


def _route_start(conn, route_ref: str):
    row = conn.execute(
        "SELECT segment_ref FROM segments WHERE route_ref=? ORDER BY seq LIMIT 1",
        (route_ref,),
    ).fetchone()
    return row["segment_ref"] if row else None


def _classify(position, closed_segments: set, trapped: set) -> str:
    if position is None:
        return "not_started"
    if position in closed_segments or position in trapped:
        return "in_affected_zone"
    return "can_reroute"


def _viable_connections(conn, base_segment: str, components, exits):
    """从 base_segment 出发、另一侧分量含有出口的开放连接，即安全替代连接。"""
    node_has_exit = {}
    for component in components:
        has_exit = bool(component & exits)
        for node in component:
            node_has_exit[node] = has_exit
    rows = conn.execute(
        "SELECT conn_ref, from_ref, to_ref, kind FROM connections "
        "WHERE status='open' AND (from_ref=? OR to_ref=?)",
        (base_segment, base_segment),
    ).fetchall()
    viable = []
    for row in rows:
        other = row["to_ref"] if row["from_ref"] == base_segment else row["from_ref"]
        if node_has_exit.get(other):
            viable.append({"conn_ref": row["conn_ref"], "to_ref": other, "kind": row["kind"]})
    return viable


# ---------------------------------------------------------------- 通告与评估

def publish_notice(conn, payload: dict) -> dict:
    _require_fields(payload, ("notice_ref", "target_kind", "target_ref", "change",
                              "effective_from", "published_by"))
    if payload["target_kind"] not in ("segment", "connection"):
        raise BadRequest("target_kind 必须是 segment 或 connection")
    if payload["change"] not in ("open", "closed"):
        raise BadRequest("change 必须是 open 或 closed")
    parse_time(payload["effective_from"], "effective_from")
    if payload.get("effective_until"):
        parse_time(payload["effective_until"], "effective_until")

    if payload["target_kind"] == "segment":
        exists = conn.execute(
            "SELECT 1 FROM segments WHERE segment_ref=?", (payload["target_ref"],)
        ).fetchone()
        if exists is None:
            raise NotFound(f"路段不存在：{payload['target_ref']}")
    else:
        exists = conn.execute(
            "SELECT 1 FROM connections WHERE conn_ref=?", (payload["target_ref"],)
        ).fetchone()
        if exists is None:
            raise NotFound(f"连接不存在：{payload['target_ref']}")

    published_at = now_iso()
    try:
        conn.execute(
            "INSERT INTO notices(notice_ref, target_kind, target_ref, change, "
            "effective_from, effective_until, published_by, published_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (payload["notice_ref"], payload["target_kind"], payload["target_ref"],
             payload["change"], payload["effective_from"],
             payload.get("effective_until"), payload["published_by"], published_at),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise Conflict(f"通告已发布：{payload['notice_ref']}")
        raise

    if payload["target_kind"] == "segment":
        conn.execute(
            "INSERT INTO segment_status(segment_ref, status, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(segment_ref) DO UPDATE SET status=excluded.status, "
            "updated_at=excluded.updated_at",
            (payload["target_ref"], payload["change"], published_at),
        )
    else:
        conn.execute(
            "UPDATE connections SET status=?, updated_at=? WHERE conn_ref=?",
            (payload["change"], published_at, payload["target_ref"]),
        )

    evaluations = _evaluate_teams(conn, payload["notice_ref"])
    return {"notice": {"notice_ref": payload["notice_ref"],
                       "target_kind": payload["target_kind"],
                       "target_ref": payload["target_ref"],
                       "change": payload["change"],
                       "published_at": published_at},
            "evaluations": evaluations}


def _evaluate_teams(conn, notice_ref: str) -> list:
    components, exits, closed_segments = _open_graph(conn)
    trapped = _trapped_zone(components, exits)
    evaluated_at = now_iso()
    results = []
    teams = conn.execute("SELECT * FROM teams ORDER BY team_ref").fetchall()
    for team in teams:
        team_ref = team["team_ref"]
        position = team_position(conn, team_ref)
        classification = _classify(position, closed_segments, trapped)
        conn.execute(
            "UPDATE reroute_offers SET status='superseded', decided_at=? "
            "WHERE team_ref=? AND status='offered'",
            (evaluated_at, team_ref),
        )
        offers = []
        if classification != "in_affected_zone":
            base = position if position else _route_start(conn, team["route_ref"])
            if base and base not in closed_segments and base not in trapped:
                for candidate in _viable_connections(conn, base, components, exits):
                    offer_ref = _new_ref("OFF")
                    conn.execute(
                        "INSERT INTO reroute_offers(offer_ref, team_ref, notice_ref, "
                        "conn_ref, status, created_at) VALUES (?,?,?,?, 'offered', ?)",
                        (offer_ref, team_ref, notice_ref, candidate["conn_ref"],
                         evaluated_at),
                    )
                    offers.append({"offer_ref": offer_ref, **candidate})
        conn.execute(
            "INSERT INTO evaluations(notice_ref, team_ref, classification, "
            "position_segment, trapped_zone, evaluated_at) VALUES (?,?,?,?,?,?)",
            (notice_ref, team_ref, classification, position,
             json.dumps(sorted(trapped), ensure_ascii=False), evaluated_at),
        )
        results.append({"team_ref": team_ref, "classification": classification,
                        "position_segment": position, "offers": offers,
                        "decision_choices": list(DECISIONS)
                        if classification == "in_affected_zone" else []})
    return results


def team_options(conn, team_ref: str) -> dict:
    _get_team(conn, team_ref)
    evaluation = conn.execute(
        "SELECT classification, position_segment, notice_ref, evaluated_at "
        "FROM evaluations WHERE team_ref=? ORDER BY id DESC LIMIT 1",
        (team_ref,),
    ).fetchone()
    offers = conn.execute(
        "SELECT offer_ref, conn_ref, created_at FROM reroute_offers "
        "WHERE team_ref=? AND status='offered'",
        (team_ref,),
    ).fetchall()
    classification = evaluation["classification"] if evaluation else None
    return {
        "team_ref": team_ref,
        "classification": classification,
        "position_segment": evaluation["position_segment"] if evaluation else None,
        "offers": [dict(row) for row in offers],
        "decision_choices": list(DECISIONS) if classification == "in_affected_zone" else [],
    }


def respond_offer(conn, offer_ref: str, payload: dict) -> dict:
    _require_fields(payload, ("action",))
    action = payload["action"]
    if action not in ("accept", "decline"):
        raise BadRequest("action 必须是 accept 或 decline")
    offer = conn.execute(
        "SELECT * FROM reroute_offers WHERE offer_ref=?", (offer_ref,)
    ).fetchone()
    if offer is None:
        raise NotFound(f"建议不存在：{offer_ref}")
    if offer["status"] != "offered":
        raise Conflict(f"建议当前状态为 {offer['status']}，无法响应")
    decided_at = now_iso()
    new_status = "accepted" if action == "accept" else "declined"
    conn.execute(
        "UPDATE reroute_offers SET status=?, decided_at=? WHERE offer_ref=?",
        (new_status, decided_at, offer_ref),
    )
    if action == "accept":
        conn.execute(
            "UPDATE reroute_offers SET status='superseded', decided_at=? "
            "WHERE team_ref=? AND status='offered'",
            (decided_at, offer["team_ref"]),
        )
    return {"offer_ref": offer_ref, "status": new_status, "decided_at": decided_at}


# ---------------------------------------------------------------- 已入谷队伍决定

def record_decision(conn, team_ref: str, payload: dict) -> dict:
    _get_team(conn, team_ref)
    _require_fields(payload, ("decision", "decided_by"))
    if payload["decision"] not in DECISIONS:
        raise BadRequest(f"decision 必须是 {DECISIONS} 之一")
    components, exits, closed_segments = _open_graph(conn)
    trapped = _trapped_zone(components, exits)
    position = team_position(conn, team_ref)
    if _classify(position, closed_segments, trapped) != "in_affected_zone":
        raise Conflict("该队伍当前不在受影响区域内，不能登记等待/折返/护送决定")
    notice_ref = payload.get("notice_ref")
    if notice_ref is None:
        latest = conn.execute(
            "SELECT notice_ref FROM notices ORDER BY published_at DESC LIMIT 1"
        ).fetchone()
        notice_ref = latest["notice_ref"] if latest else None
    decided_at = now_iso()
    cursor = conn.execute(
        "INSERT INTO decisions(team_ref, notice_ref, decision, decided_by, decided_at, note) "
        "VALUES (?,?,?,?,?,?)",
        (team_ref, notice_ref, payload["decision"], payload["decided_by"],
         decided_at, payload.get("note")),
    )
    return {"id": cursor.lastrowid, "team_ref": team_ref, "notice_ref": notice_ref,
            "decision": payload["decision"], "decided_at": decided_at}


# ---------------------------------------------------------------- 出口与接驳占用

def create_booking(conn, payload: dict) -> dict:
    _require_fields(payload, ("team_ref", "exit_ref", "vehicle_ref",
                              "window_start", "window_end"))
    _get_team(conn, payload["team_ref"])
    exit_row = conn.execute(
        "SELECT exit_ref FROM exits WHERE exit_ref=?", (payload["exit_ref"],)
    ).fetchone()
    if exit_row is None:
        raise NotFound(f"出口不存在：{payload['exit_ref']}")
    start = parse_time(payload["window_start"], "window_start")
    end = parse_time(payload["window_end"], "window_end")
    if not start < end:
        raise BadRequest("window_start 必须早于 window_end")

    # BEGIN IMMEDIATE 保证并发下“检查 + 写入”原子，出口与车辆不会被重复占用；
    # 若外层已有事务（如测试场景），则直接复用外层事务。
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT booking_ref, team_ref, exit_ref, vehicle_ref, window_start, window_end "
            "FROM bookings WHERE status='active' AND (exit_ref=? OR vehicle_ref=?)",
            (payload["exit_ref"], payload["vehicle_ref"]),
        ).fetchall()
        for row in rows:
            other_start = parse_time(row["window_start"], "window_start")
            other_end = parse_time(row["window_end"], "window_end")
            if start < other_end and end > other_start:
                resource = ("出口" if row["exit_ref"] == payload["exit_ref"] else "接驳车辆")
                raise Conflict(
                    f"{resource}在该时段已被队伍 {row['team_ref']} 占用"
                    f"（预订 {row['booking_ref']}）"
                )
        booking_ref = _new_ref("BK")
        conn.execute(
            "INSERT INTO bookings(booking_ref, team_ref, exit_ref, vehicle_ref, "
            "window_start, window_end, status, created_at) "
            "VALUES (?,?,?,?,?,?, 'active', ?)",
            (booking_ref, payload["team_ref"], payload["exit_ref"],
             payload["vehicle_ref"], start.isoformat(timespec="seconds"),
             end.isoformat(timespec="seconds"), now_iso()),
        )
        if own_txn:
            conn.commit()
    except Exception:
        if own_txn:
            conn.rollback()
        raise
    return {"booking_ref": booking_ref, "team_ref": payload["team_ref"],
            "exit_ref": payload["exit_ref"], "vehicle_ref": payload["vehicle_ref"],
            "window_start": start.isoformat(timespec="seconds"),
            "window_end": end.isoformat(timespec="seconds"), "status": "active"}


def release_booking(conn, booking_ref: str) -> dict:
    row = conn.execute(
        "SELECT * FROM bookings WHERE booking_ref=?", (booking_ref,)
    ).fetchone()
    if row is None:
        raise NotFound(f"预订不存在：{booking_ref}")
    if row["status"] != "active":
        raise Conflict(f"预订当前状态为 {row['status']}，无法释放")
    conn.execute(
        "UPDATE bookings SET status='released' WHERE booking_ref=?", (booking_ref,)
    )
    return {"booking_ref": booking_ref, "status": "released"}


# ---------------------------------------------------------------- 复盘

_MEANS_TEXT = {
    "reroute": "经替代连接 {conn_ref} 改线",
    "wait": "就地等待后恢复通行",
    "retreat": "沿来路折返",
    "escort": "由维护人员人工护送离场",
    "departed": "按接驳预订离场",
}


def _exit_account(means: str, offer, decision, booking) -> str:
    parts = []
    if means == "reroute" and offer:
        parts.append(_MEANS_TEXT["reroute"].format(conn_ref=offer["conn_ref"]))
    elif means in ("wait", "retreat", "escort"):
        parts.append(_MEANS_TEXT[means])
    elif means == "departed":
        parts.append(_MEANS_TEXT["departed"])
    if booking:
        parts.append(
            f"自 {booking['exit_ref']} 乘接驳车 {booking['vehicle_ref']}"
            f"（{booking['window_start']} 至 {booking['window_end']}）"
        )
    else:
        parts.append("尚未登记离场接驳")
    if not parts or (means == "unresolved" and not booking):
        return "尚未记录离场方式"
    return "，".join(parts)


def review(conn, notice_ref=None) -> dict:
    if notice_ref is None:
        latest = conn.execute(
            "SELECT notice_ref FROM notices ORDER BY published_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            raise NotFound("尚无通告，无法复盘")
        notice_ref = latest["notice_ref"]
    notice = conn.execute(
        "SELECT * FROM notices WHERE notice_ref=?", (notice_ref,)
    ).fetchone()
    if notice is None:
        raise NotFound(f"通告不存在：{notice_ref}")

    evaluations = conn.execute(
        "SELECT * FROM evaluations WHERE notice_ref=? ORDER BY team_ref",
        (notice_ref,),
    ).fetchall()
    teams_out = []
    for evaluation in evaluations:
        team_ref = evaluation["team_ref"]
        offers = conn.execute(
            "SELECT offer_ref, conn_ref, status FROM reroute_offers "
            "WHERE team_ref=? AND notice_ref=? ORDER BY created_at",
            (team_ref, notice_ref),
        ).fetchall()
        accepted = next((o for o in offers if o["status"] == "accepted"), None)
        # 最终决定可能登记在本次通告之后，复盘取该队伍最新的决定与接驳预订。
        decision = conn.execute(
            "SELECT decision, decided_by, decided_at, note FROM decisions "
            "WHERE team_ref=? ORDER BY decided_at DESC, id DESC LIMIT 1",
            (team_ref,),
        ).fetchone()
        booking = conn.execute(
            "SELECT booking_ref, exit_ref, vehicle_ref, window_start, window_end, status "
            "FROM bookings WHERE team_ref=? ORDER BY created_at DESC LIMIT 1",
            (team_ref,),
        ).fetchone()
        if accepted:
            means = "reroute"
        elif decision:
            means = decision["decision"]
        elif booking:
            means = "departed"
        else:
            means = "unresolved"
        teams_out.append({
            "team_ref": team_ref,
            "classification": evaluation["classification"],
            "position_segment": evaluation["position_segment"],
            "offers": [dict(row) for row in offers],
            "decision": dict(decision) if decision else None,
            "booking": dict(booking) if booking else None,
            "exit_means": means,
            "exit_account": _exit_account(means, accepted, decision, booking),
        })
    return {
        "notice": {"notice_ref": notice["notice_ref"],
                   "target_kind": notice["target_kind"],
                   "target_ref": notice["target_ref"],
                   "change": notice["change"],
                   "published_by": notice["published_by"],
                   "published_at": notice["published_at"]},
        "trapped_zone": json.loads(evaluations[0]["trapped_zone"]) if evaluations else [],
        "teams": teams_out,
    }
