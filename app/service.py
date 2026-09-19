
"""分段行程接续领域服务。

时间约定：外部传入的时间必须是带偏移的 ISO 8601 字符串。
库内同时保存原始字符串（*_raw）与归一化到 UTC 的字符串，
原始时间用于复盘举证，UTC 时间用于排序与时间窗重叠判定。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .db import session

STAFF_ROLE = "MAINTENANCE"
BACKFILL_TOLERANCE_SECONDS = 60


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _bad(message: str) -> ServiceError:
    return ServiceError(400, "VALIDATION_ERROR", message)


def _unauthorized(message: str = "缺少身份信息") -> ServiceError:
    return ServiceError(401, "UNAUTHENTICATED", message)


def _forbidden(message: str) -> ServiceError:
    return ServiceError(403, "FORBIDDEN", message)


def _not_found(message: str) -> ServiceError:
    return ServiceError(404, "NOT_FOUND", message)


def _conflict(message: str) -> ServiceError:
    return ServiceError(409, "CONFLICT", message)


def now_iso(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(timezone.utc).isoformat()


def parse_time(value: str, field: str) -> tuple[str, datetime]:
    """返回（原始字符串，UTC datetime）；拒绝不带时区偏移的时间。"""
    if not isinstance(value, str) or not value:
        raise _bad(f"{field} 必须是带偏移的 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _bad(f"{field} 不是合法的 ISO 8601 时间：{value}") from exc
    if parsed.tzinfo is None:
        raise _bad(f"{field} 必须带时区偏移，例如 2026-09-19T10:00:00+08:00")
    return value, parsed.astimezone(timezone.utc)


class Service:
    def __init__(
        self,
        database_path: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = database_path
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def now(self) -> str:
        return now_iso(self.clock)

    # ---- 身份与基础查询 -------------------------------------------------

    def _require_leader(self, leader_ref: str | None) -> str:
        if not leader_ref:
            raise _unauthorized("缺少 X-Leader-Ref")
        return leader_ref

    def require_staff(self, staff_role: str | None) -> None:
        if staff_role != STAFF_ROLE:
            raise _unauthorized("需要维护人员身份（X-Staff-Role: MAINTENANCE）")

    def _owned_team(self, conn: sqlite3.Connection, team_ref: str, leader_ref: str) -> sqlite3.Row:
        team = conn.execute("SELECT * FROM teams WHERE team_ref = ?", (team_ref,)).fetchone()
        if team is None:
            raise _not_found(f"队伍 {team_ref} 不存在")
        if team["leader_ref"] != leader_ref:
            raise _forbidden("只有所属带队人员可以操作本队行程")
        return team

    # ---- 基础数据登记（维护人员） ---------------------------------------

    def register_leader(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        ref = _require_str(data, "leader_ref")
        name = _require_str(data, "display_name")
        with session(self.database_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO leaders(leader_ref, display_name, contact, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (ref, name, data.get("contact"), self.now),
                )
            except sqlite3.IntegrityError as exc:
                raise _conflict(f"带队人员 {ref} 已存在") from exc
        return {"leader_ref": ref}

    def register_team(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        team_ref = _require_str(data, "team_ref")
        name = _require_str(data, "display_name")
        leader_ref = _require_str(data, "leader_ref")
        level = _require_str(data, "trail_level")
        if level not in ("BEGINNER", "INTERMEDIATE", "ADVANCED"):
            raise _bad("trail_level 必须是 BEGINNER / INTERMEDIATE / ADVANCED")
        visitors = data.get("visitors", [])
        if not isinstance(visitors, list):
            raise _bad("visitors 必须是数组")
        with session(self.database_path) as conn:
            if conn.execute("SELECT 1 FROM leaders WHERE leader_ref = ?", (leader_ref,)).fetchone() is None:
                raise _bad(f"带队人员 {leader_ref} 尚未登记")
            try:
                conn.execute(
                    "INSERT INTO teams(team_ref, display_name, leader_ref, trail_level, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (team_ref, name, leader_ref, level, self.now),
                )
            except sqlite3.IntegrityError as exc:
                raise _conflict(f"队伍 {team_ref} 已存在") from exc
            for item in visitors:
                conn.execute(
                    "INSERT INTO visitors(team_ref, visitor_ref, display_name, note)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        team_ref,
                        _require_str(item, "visitor_ref"),
                        _require_str(item, "display_name"),
                        item.get("note"),
                    ),
                )
        return {"team_ref": team_ref, "visitors": len(visitors)}

    def register_segment(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        segment_ref = _require_str(data, "segment_ref")
        route_ref = _require_str(data, "route_ref")
        capacity = data.get("capacity")
        if not isinstance(capacity, int) or capacity <= 0:
            raise _bad("capacity 必须是正整数")
        position = data.get("position", 0)
        if not isinstance(position, int):
            raise _bad("position 必须是整数")
        with session(self.database_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO segments(segment_ref, route_ref, display_name, position, in_valley, capacity)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        segment_ref,
                        route_ref,
                        _require_str(data, "display_name"),
                        position,
                        1 if data.get("in_valley") else 0,
                        capacity,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise _conflict(f"路段 {segment_ref} 已存在") from exc
        return {"segment_ref": segment_ref}

    def register_exit(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        ref = _require_str(data, "exit_ref")
        with session(self.database_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO exits(exit_ref, display_name) VALUES (?, ?)",
                    (ref, _require_str(data, "display_name")),
                )
            except sqlite3.IntegrityError as exc:
                raise _conflict(f"出口 {ref} 已存在") from exc
        return {"exit_ref": ref}

    def register_shuttle(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        ref = _require_str(data, "shuttle_ref")
        capacity = data.get("capacity")
        if not isinstance(capacity, int) or capacity <= 0:
            raise _bad("capacity 必须是正整数")
        with session(self.database_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO shuttles(shuttle_ref, display_name, capacity) VALUES (?, ?, ?)",
                    (ref, _require_str(data, "display_name"), capacity),
                )
            except sqlite3.IntegrityError as exc:
                raise _conflict(f"接驳车辆 {ref} 已存在") from exc
        return {"shuttle_ref": ref}

    def register_plan(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        team_ref = _require_str(data, "team_ref")
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise _bad("items 必须是非空数组")
        parsed: list[tuple[str, str, str]] = []
        for item in items:
            segment_ref = _require_str(item, "segment_ref")
            _, start = parse_time(_require_str(item, "window_start"), "window_start")
            _, end = parse_time(_require_str(item, "window_end"), "window_end")
            if end <= start:
                raise _bad("window_end 必须晚于 window_start")
            parsed.append((segment_ref, start.isoformat(), end.isoformat()))
        with session(self.database_path) as conn:
            if conn.execute("SELECT 1 FROM teams WHERE team_ref = ?", (team_ref,)).fetchone() is None:
                raise _not_found(f"队伍 {team_ref} 不存在")
            for segment_ref, start, end in parsed:
                if conn.execute("SELECT 1 FROM segments WHERE segment_ref = ?", (segment_ref,)).fetchone() is None:
                    raise _not_found(f"路段 {segment_ref} 不存在")
                conn.execute(
                    "INSERT INTO team_plans(team_ref, segment_ref, window_start, window_end)"
                    " VALUES (?, ?, ?, ?)",
                    (team_ref, segment_ref, start, end),
                )
        return {"team_ref": team_ref, "segments": len(parsed)}

    # ---- 签到（支持断网补报） -------------------------------------------

    def record_checkin(
        self, leader_ref: str | None, team_ref: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        leader_ref = self._require_leader(leader_ref)
        segment_ref = _require_str(data, "segment_ref")
        action = _require_str(data, "action")
        if action not in ("ENTER", "LEAVE"):
            raise _bad("action 必须是 ENTER 或 LEAVE")
        uploaded_at = self.now
        raw_occurred = data.get("occurred_at")
        if raw_occurred is None:
            occurred_raw, occurred_dt = uploaded_at, datetime.fromisoformat(uploaded_at)
            is_backfill = False
        else:
            occurred_raw, occurred_dt = parse_time(raw_occurred, "occurred_at")
            delay = (
                datetime.fromisoformat(uploaded_at) - occurred_dt
            ).total_seconds()
            is_backfill = abs(delay) > BACKFILL_TOLERANCE_SECONDS
        with session(self.database_path) as conn:
            self._owned_team(conn, team_ref, leader_ref)
            if conn.execute("SELECT 1 FROM segments WHERE segment_ref = ?", (segment_ref,)).fetchone() is None:
                raise _not_found(f"路段 {segment_ref} 不存在")
            cur = conn.execute(
                "INSERT INTO checkin_events(team_ref, segment_ref, action,"
                " occurred_at_raw, occurred_at, uploaded_at, is_backfill, recorded_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    team_ref,
                    segment_ref,
                    action,
                    occurred_raw,
                    occurred_dt.isoformat(),
                    uploaded_at,
                    1 if is_backfill else 0,
                    leader_ref,
                ),
            )
            event_id = cur.lastrowid
        return {
            "id": event_id,
            "team_ref": team_ref,
            "segment_ref": segment_ref,
            "action": action,
            "occurred_at": occurred_raw,
            "uploaded_at": uploaded_at,
            "is_backfill": is_backfill,
        }

    # ---- 占用视图与名单 -------------------------------------------------

    def _replay_locations(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        """重放全部签到，得到每队当前所在路段。"""
        current: dict[str, dict[str, Any]] = {}
        rows = conn.execute(
            "SELECT * FROM checkin_events ORDER BY occurred_at, id"
        ).fetchall()
        for row in rows:
            if row["action"] == "ENTER":
                current[row["team_ref"]] = {
                    "segment_ref": row["segment_ref"],
                    "entered_at": row["occurred_at"],
                    "entered_at_raw": row["occurred_at_raw"],
                    "event_id": row["id"],
                }
            else:
                state = current.get(row["team_ref"])
                if state and state["segment_ref"] == row["segment_ref"]:
                    current.pop(row["team_ref"], None)
        return current

    def team_location(self, conn: sqlite3.Connection, team_ref: str) -> dict[str, Any] | None:
        return self._replay_locations(conn).get(team_ref)

    def _segment_closed(self, conn: sqlite3.Connection, segment_ref: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM closures WHERE segment_ref = ? AND active = 1", (segment_ref,)
        ).fetchone()

    def occupancy_view(self, leader_ref: str | None, staff_role: str | None) -> dict[str, Any]:
        if not leader_ref and staff_role != STAFF_ROLE:
            raise _unauthorized()
        with session(self.database_path) as conn:
            locations = self._replay_locations(conn)
            segment_counts: dict[str, int] = {}
            for state in locations.values():
                segment_counts[state["segment_ref"]] = segment_counts.get(state["segment_ref"], 0) + 1
            segments = {
                row["segment_ref"]: dict(row)
                for row in conn.execute("SELECT * FROM segments").fetchall()
            }
            teams_out = []
            for team in conn.execute("SELECT * FROM teams ORDER BY team_ref").fetchall():
                state = locations.get(team["team_ref"])
                current = None
                if state:
                    segment = segments[state["segment_ref"]]
                    current = {
                        "segment_ref": state["segment_ref"],
                        "display_name": segment["display_name"],
                        "in_valley": bool(segment["in_valley"]),
                        "entered_at": state["entered_at_raw"],
                    }
                teams_out.append(
                    {
                        "team_ref": team["team_ref"],
                        "display_name": team["display_name"],
                        "trail_level": team["trail_level"],
                        "leader_ref": team["leader_ref"],
                        "current": current,
                    }
                )
            segments_out = []
            for ref, segment in segments.items():
                segments_out.append(
                    {
                        "segment_ref": ref,
                        "route_ref": segment["route_ref"],
                        "display_name": segment["display_name"],
                        "in_valley": bool(segment["in_valley"]),
                        "capacity": segment["capacity"],
                        "occupied_teams": segment_counts.get(ref, 0),
                        "closed": self._segment_closed(conn, ref) is not None,
                    }
                )
            ropeways = [
                dict(row)
                for row in conn.execute(
                    "SELECT rs.* FROM ropeway_status rs JOIN ("
                    " SELECT facility_ref, MAX(id) AS max_id FROM ropeway_status GROUP BY facility_ref"
                    " ) latest ON latest.max_id = rs.id"
                ).fetchall()
            ]
        return {"teams": teams_out, "segments": segments_out, "ropeways": ropeways}

    def list_visitors(self, leader_ref: str | None, team_ref: str) -> dict[str, Any]:
        leader_ref = self._require_leader(leader_ref)
        with session(self.database_path) as conn:
            self._owned_team(conn, team_ref, leader_ref)
            rows = conn.execute(
                "SELECT visitor_ref, display_name, note FROM visitors WHERE team_ref = ? ORDER BY visitor_ref",
                (team_ref,),
            ).fetchall()
        return {"team_ref": team_ref, "visitors": [dict(row) for row in rows]}

    # ---- 维护人员发布 ---------------------------------------------------

    def publish_ropeway_status(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        facility_ref = _require_str(data, "facility_ref")
        status = _require_str(data, "status")
        if status not in ("OPEN", "SUSPENDED"):
            raise _bad("status 必须是 OPEN 或 SUSPENDED")
        if data.get("changed_at"):
            _, changed_dt = parse_time(data["changed_at"], "changed_at")
            changed_at = changed_dt.isoformat()
        else:
            changed_at = self.now
        with session(self.database_path) as conn:
            cur = conn.execute(
                "INSERT INTO ropeway_status(facility_ref, status, changed_at, notice, changed_by)"
                " VALUES (?, ?, ?, ?, ?)",
                (facility_ref, status, changed_at, data.get("notice"), STAFF_ROLE),
            )
        return {"id": cur.lastrowid, "facility_ref": facility_ref, "status": status}

    def publish_closure(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        segment_ref = _require_str(data, "segment_ref")
        with session(self.database_path) as conn:
            if conn.execute("SELECT 1 FROM segments WHERE segment_ref = ?", (segment_ref,)).fetchone() is None:
                raise _not_found(f"路段 {segment_ref} 不存在")
            if self._segment_closed(conn, segment_ref):
                raise _conflict(f"路段 {segment_ref} 已处于封闭状态")
            cur = conn.execute(
                "INSERT INTO closures(segment_ref, reason, opened_at, published_by)"
                " VALUES (?, ?, ?, ?)",
                (segment_ref, data.get("reason"), self.now, STAFF_ROLE),
            )
        return {"id": cur.lastrowid, "segment_ref": segment_ref, "active": True}

    def lift_closure(self, staff_role: str | None, closure_id: int) -> dict[str, Any]:
        self.require_staff(staff_role)
        with session(self.database_path) as conn:
            row = conn.execute("SELECT * FROM closures WHERE id = ?", (closure_id,)).fetchone()
            if row is None:
                raise _not_found(f"封闭记录 {closure_id} 不存在")
            if not row["active"]:
                raise _conflict("该封闭已解除")
            conn.execute(
                "UPDATE closures SET active = 0, lifted_at = ? WHERE id = ?",
                (self.now, closure_id),
            )
        return {"id": closure_id, "active": False}

    def publish_evacuation_channel(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        ref = _require_str(data, "channel_ref")
        segment_ref = _require_str(data, "from_segment_ref")
        with session(self.database_path) as conn:
            if conn.execute("SELECT 1 FROM segments WHERE segment_ref = ?", (segment_ref,)).fetchone() is None:
                raise _not_found(f"路段 {segment_ref} 不存在")
            exit_ref = data.get("to_exit_ref")
            if exit_ref and conn.execute("SELECT 1 FROM exits WHERE exit_ref = ?", (exit_ref,)).fetchone() is None:
                raise _not_found(f"出口 {exit_ref} 不存在")
            try:
                conn.execute(
                    "INSERT INTO evacuation_channels(channel_ref, from_segment_ref, direction,"
                    " to_exit_ref, open, published_at, published_by)"
                    " VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (
                        ref,
                        segment_ref,
                        _require_str(data, "direction"),
                        exit_ref,
                        self.now,
                        STAFF_ROLE,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise _conflict(f"疏散通道 {ref} 已存在") from exc
        return {"channel_ref": ref, "open": True}

    def set_evacuation_channel(self, staff_role: str | None, channel_ref: str, open_flag: bool) -> dict[str, Any]:
        self.require_staff(staff_role)
        with session(self.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM evacuation_channels WHERE channel_ref = ?", (channel_ref,)
            ).fetchone()
            if row is None:
                raise _not_found(f"疏散通道 {channel_ref} 不存在")
            conn.execute(
                "UPDATE evacuation_channels SET open = ? WHERE channel_ref = ?",
                (1 if open_flag else 0, channel_ref),
            )
        return {"channel_ref": channel_ref, "open": open_flag}

    def publish_alternative(self, staff_role: str | None, data: dict[str, Any]) -> dict[str, Any]:
        self.require_staff(staff_role)
        ref = _require_str(data, "connection_ref")
        from_ref = _require_str(data, "from_segment_ref")
        to_ref = _require_str(data, "to_segment_ref")
        kind = _require_str(data, "kind")
        level = data.get("trail_level")
        if level is not None and level not in ("BEGINNER", "INTERMEDIATE", "ADVANCED"):
            raise _bad("trail_level 必须是 BEGINNER / INTERMEDIATE / ADVANCED 或空")
        with session(self.database_path) as conn:
            for seg in (from_ref, to_ref):
                if conn.execute("SELECT 1 FROM segments WHERE segment_ref = ?", (seg,)).fetchone() is None:
                    raise _not_found(f"路段 {seg} 不存在")
            try:
                conn.execute(
                    "INSERT INTO alternative_connections(connection_ref, from_segment_ref, to_segment_ref,"
                    " kind, trail_level, active, published_at, published_by, note)"
                    " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (ref, from_ref, to_ref, kind, level, self.now, STAFF_ROLE, data.get("note")),
                )
            except sqlite3.IntegrityError as exc:
                raise _conflict(f"替代连接 {ref} 已存在") from exc
        return {"connection_ref": ref, "active": True}

    # ---- 队伍改线 / 入谷处置 --------------------------------------------

    def _staging_segment(self, conn: sqlite3.Connection, team: sqlite3.Row) -> dict[str, Any] | None:
        """队伍当前所在路段；尚无签到时取计划中最早的路段。"""
        location = self.team_location(conn, team["team_ref"])
        if location:
            return location
        plan = conn.execute(
            "SELECT segment_ref FROM team_plans WHERE team_ref = ? ORDER BY window_start, id LIMIT 1",
            (team["team_ref"],),
        ).fetchone()
        if plan:
            return {"segment_ref": plan["segment_ref"], "entered_at": None, "entered_at_raw": None}
        return None

    def list_alternatives(self, leader_ref: str | None, team_ref: str) -> dict[str, Any]:
        leader_ref = self._require_leader(leader_ref)
        with session(self.database_path) as conn:
            team = self._owned_team(conn, team_ref, leader_ref)
            location = self.team_location(conn, team_ref)
            if location:
                segment = conn.execute(
                    "SELECT * FROM segments WHERE segment_ref = ?", (location["segment_ref"],)
                ).fetchone()
                if segment["in_valley"]:
                    raise _conflict("队伍已进入山谷，不能改线；只能登记等待、折返或人工护送决定")
            staging = self._staging_segment(conn, team)
            if staging is None:
                return {"team_ref": team_ref, "in_valley": False, "alternatives": []}
            candidates = conn.execute(
                "SELECT * FROM alternative_connections WHERE active = 1 AND from_segment_ref = ?",
                (staging["segment_ref"],),
            ).fetchall()
            result = []
            for row in candidates:
                if row["trail_level"] and row["trail_level"] != team["trail_level"]:
                    continue
                if self._segment_closed(conn, row["from_segment_ref"]) or self._segment_closed(
                    conn, row["to_segment_ref"]
                ):
                    continue
                result.append(
                    {
                        "connection_ref": row["connection_ref"],
                        "from_segment_ref": row["from_segment_ref"],
                        "to_segment_ref": row["to_segment_ref"],
                        "kind": row["kind"],
                        "trail_level": row["trail_level"],
                        "note": row["note"],
                    }
                )
        return {"team_ref": team_ref, "in_valley": False, "alternatives": result}

    def decide_reroute(self, leader_ref: str | None, team_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        leader_ref = self._require_leader(leader_ref)
        connection_ref = _require_str(data, "connection_ref")
        with session(self.database_path) as conn:
            team = self._owned_team(conn, team_ref, leader_ref)
            location = self.team_location(conn, team_ref)
            if location:
                segment = conn.execute(
                    "SELECT in_valley FROM segments WHERE segment_ref = ?",
                    (location["segment_ref"],),
                ).fetchone()
                if segment["in_valley"]:
                    raise _conflict("已入谷队伍不能改线，请登记等待、折返或人工护送决定")
            connection = conn.execute(
                "SELECT * FROM alternative_connections WHERE connection_ref = ? AND active = 1",
                (connection_ref,),
            ).fetchone()
            if connection is None:
                raise _not_found(f"替代连接 {connection_ref} 不存在或已停用")
            if connection["trail_level"] and connection["trail_level"] != team["trail_level"]:
                raise _conflict("该替代连接不适合本队步道等级")
            staging = self._staging_segment(conn, team)
            if staging is None or staging["segment_ref"] != connection["from_segment_ref"]:
                raise _conflict("替代连接起点与队伍当前/计划路段不符")
            if self._segment_closed(conn, connection["from_segment_ref"]) or self._segment_closed(
                conn, connection["to_segment_ref"]
            ):
                raise _conflict("替代连接经停路段已封闭，不能安全改线")
            if conn.execute(
                "SELECT 1 FROM reroute_decisions WHERE team_ref = ?", (team_ref,)
            ).fetchone():
                raise _conflict("本队已有改线决定")
            conn.execute(
                "INSERT INTO reroute_decisions(team_ref, connection_ref, decided_at, decided_by, note)"
                " VALUES (?, ?, ?, ?, ?)",
                (team_ref, connection_ref, self.now, leader_ref, data.get("note")),
            )
        return {"team_ref": team_ref, "connection_ref": connection_ref, "decision": "REROUTE"}

    def decide_valley_action(
        self, leader_ref: str | None, team_ref: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        leader_ref = self._require_leader(leader_ref)
        decision = _require_str(data, "decision")
        if decision not in ("WAIT", "RETURN", "ESCORT"):
            raise _bad("decision 必须是 WAIT / RETURN / ESCORT")
        with session(self.database_path) as conn:
            self._owned_team(conn, team_ref, leader_ref)
            location = self.team_location(conn, team_ref)
            if location is None:
                raise _conflict("队伍当前不在任何路段上")
            segment = conn.execute(
                "SELECT in_valley FROM segments WHERE segment_ref = ?",
                (location["segment_ref"],),
            ).fetchone()
            if not segment["in_valley"]:
                raise _conflict("队伍尚未入谷，应使用替代连接改线而非入谷处置决定")
            conn.execute(
                "UPDATE valley_decisions SET superseded = 1 WHERE team_ref = ? AND superseded = 0",
                (team_ref,),
            )
            cur = conn.execute(
                "INSERT INTO valley_decisions(team_ref, decision, decided_at, decided_by, note)"
                " VALUES (?, ?, ?, ?, ?)",
                (team_ref, decision, self.now, leader_ref, data.get("note")),
            )
        return {"id": cur.lastrowid, "team_ref": team_ref, "decision": decision}

    # ---- 出口与接驳车辆占用 ---------------------------------------------

    def _overlap(self, conn: sqlite3.Connection, column: str, ref: str, start: str, end: str,
                 team_ref: str) -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT r.*, t.display_name AS team_name FROM reservations r"
            f" JOIN teams t ON t.team_ref = r.team_ref"
            f" WHERE r.{column} = ? AND r.status = 'CLAIMED' AND r.team_ref != ?"
            f" AND r.window_start < ? AND ? < r.window_end",
            (ref, team_ref, end, start),
        ).fetchone()

    def claim_reservation(
        self, leader_ref: str | None, team_ref: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        leader_ref = self._require_leader(leader_ref)
        exit_ref = data.get("exit_ref")
        shuttle_ref = data.get("shuttle_ref")
        if not exit_ref and not shuttle_ref:
            raise _bad("exit_ref 与 shuttle_ref 至少提供一个")
        _, start_dt = parse_time(_require_str(data, "window_start"), "window_start")
        _, end_dt = parse_time(_require_str(data, "window_end"), "window_end")
        if end_dt <= start_dt:
            raise _bad("window_end 必须晚于 window_start")
        start, end = start_dt.isoformat(), end_dt.isoformat()
        with session(self.database_path) as conn:
            self._owned_team(conn, team_ref, leader_ref)
            if exit_ref and conn.execute("SELECT 1 FROM exits WHERE exit_ref = ?", (exit_ref,)).fetchone() is None:
                raise _not_found(f"出口 {exit_ref} 不存在")
            if shuttle_ref and conn.execute(
                "SELECT 1 FROM shuttles WHERE shuttle_ref = ?", (shuttle_ref,)
            ).fetchone() is None:
                raise _not_found(f"接驳车辆 {shuttle_ref} 不存在")
            if exit_ref:
                clash = self._overlap(conn, "exit_ref", exit_ref, start, end, team_ref)
                if clash:
                    raise _conflict(
                        f"出口 {exit_ref} 在该时段已被队伍 {clash['team_ref']}（{clash['team_name']}）占用"
                    )
            if shuttle_ref:
                clash = self._overlap(conn, "shuttle_ref", shuttle_ref, start, end, team_ref)
                if clash:
                    raise _conflict(
                        f"接驳车辆 {shuttle_ref} 在该时段已被队伍 {clash['team_ref']}（{clash['team_name']}）占用"
                    )
            cur = conn.execute(
                "INSERT INTO reservations(team_ref, exit_ref, shuttle_ref, window_start, window_end,"
                " status, created_at, created_by) VALUES (?, ?, ?, ?, ?, 'CLAIMED', ?, ?)",
                (team_ref, exit_ref, shuttle_ref, start, end, self.now, leader_ref),
            )
        return {
            "id": cur.lastrowid,
            "team_ref": team_ref,
            "exit_ref": exit_ref,
            "shuttle_ref": shuttle_ref,
            "window_start": start,
            "window_end": end,
            "status": "CLAIMED",
        }

    def release_reservation(self, leader_ref: str | None, reservation_id: int) -> dict[str, Any]:
        leader_ref = self._require_leader(leader_ref)
        with session(self.database_path) as conn:
            row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
            if row is None:
                raise _not_found(f"占用记录 {reservation_id} 不存在")
            self._owned_team(conn, row["team_ref"], leader_ref)
            if row["status"] != "CLAIMED":
                raise _conflict("该占用已释放")
            conn.execute(
                "UPDATE reservations SET status = 'RELEASED' WHERE id = ?", (reservation_id,)
            )
        return {"id": reservation_id, "status": "RELEASED"}

    # ---- 复盘 -----------------------------------------------------------

    def record_outcome(self, leader_ref: str | None, team_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        leader_ref = self._require_leader(leader_ref)
        how = _require_str(data, "how")
        if how not in ("REROUTED", "WAITED_AND_RESUMED", "TURNED_BACK", "ESCORTED"):
            raise _bad("how 取值不合法")
        raw_left, left_dt = parse_time(_require_str(data, "left_affected_at"), "left_affected_at")
        exit_ref = data.get("exit_ref")
        shuttle_ref = data.get("shuttle_ref")
        with session(self.database_path) as conn:
            self._owned_team(conn, team_ref, leader_ref)
            reroute = conn.execute(
                "SELECT * FROM reroute_decisions WHERE team_ref = ?", (team_ref,)
            ).fetchone()
            valley = conn.execute(
                "SELECT * FROM valley_decisions WHERE team_ref = ? AND superseded = 0",
                (team_ref,),
            ).fetchone()
            if how == "REROUTED" and reroute is None:
                raise _conflict("缺少改线决定，不能记录为 REROUTED")
            if how == "WAITED_AND_RESUMED" and (valley is None or valley["decision"] != "WAIT"):
                raise _conflict("缺少当前有效的 WAIT 决定，不能记录为 WAITED_AND_RESUMED")
            if how == "TURNED_BACK" and (valley is None or valley["decision"] != "RETURN"):
                raise _conflict("缺少当前有效的 RETURN 决定，不能记录为 TURNED_BACK")
            if how == "ESCORTED" and (valley is None or valley["decision"] != "ESCORT"):
                raise _conflict("缺少当前有效的 ESCORT 决定，不能记录为 ESCORTED")
            if exit_ref and conn.execute("SELECT 1 FROM exits WHERE exit_ref = ?", (exit_ref,)).fetchone() is None:
                raise _not_found(f"出口 {exit_ref} 不存在")
            if shuttle_ref and conn.execute(
                "SELECT 1 FROM shuttles WHERE shuttle_ref = ?", (shuttle_ref,)
            ).fetchone() is None:
                raise _not_found(f"接驳车辆 {shuttle_ref} 不存在")
            try:
                conn.execute(
                    "INSERT INTO evacuation_outcomes(team_ref, how, exit_ref, shuttle_ref,"
                    " left_affected_at_raw, left_affected_at, recorded_at, note)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        team_ref,
                        how,
                        exit_ref,
                        shuttle_ref,
                        raw_left,
                        left_dt.isoformat(),
                        self.now,
                        data.get("note"),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise _conflict("本队复盘结果已记录") from exc
        return {"team_ref": team_ref, "how": how, "left_affected_at": raw_left}

    def review_outcomes(self, staff_role: str | None) -> dict[str, Any]:
        self.require_staff(staff_role)
        with session(self.database_path) as conn:
            outcomes = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM evacuation_outcomes ORDER BY left_affected_at, team_ref"
                ).fetchall()
            ]
            accounted = {row["team_ref"] for row in outcomes}
            pending = [
                row["team_ref"]
                for row in conn.execute("SELECT team_ref FROM teams ORDER BY team_ref").fetchall()
                if row["team_ref"] not in accounted
            ]
        return {"outcomes": outcomes, "teams_without_outcome": pending}


def _require_str(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{field} 不能为空")
    return value
