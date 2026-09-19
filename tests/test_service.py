
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.main import Application
from app.service import Service
from scripts.migrate import migrate

STAFF = {"X-Staff-Role": "MAINTENANCE"}
T0 = datetime(2026, 9, 19, 8, 0, tzinfo=timezone(timedelta(hours=8)))


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, minutes: int) -> None:
        self.value += timedelta(minutes=minutes)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite3"
        migrate(self.db_path)
        self.clock = FakeClock(T0)
        self.app = Application(Service(str(self.db_path), clock=self.clock))
        self._seed()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, method: str, path: str, headers=None, body=None):
        return self.app.handle(method, path, headers or {}, body)

    def _post(self, path: str, body, headers=None):
        import json
        return self.call("POST", path, headers, json.dumps(body).encode("utf-8"))

    def _seed(self) -> None:
        self.assertEqual(self._post("/api/staff/leaders", {"leader_ref": "L-A", "display_name": "甲带队"}, STAFF)[0], 201)
        self.assertEqual(self._post("/api/staff/leaders", {"leader_ref": "L-B", "display_name": "乙带队"}, STAFF)[0], 201)
        self.assertEqual(
            self._post(
                "/api/staff/teams",
                {
                    "team_ref": "TEAM-A",
                    "display_name": "初级一队",
                    "leader_ref": "L-A",
                    "trail_level": "BEGINNER",
                    "visitors": [
                        {"visitor_ref": "V1", "display_name": "访客甲"},
                        {"visitor_ref": "V2", "display_name": "访客乙"},
                    ],
                },
                STAFF,
            )[0],
            201,
        )
        self.assertEqual(
            self._post(
                "/api/staff/teams",
                {
                    "team_ref": "TEAM-B",
                    "display_name": "高级二队",
                    "leader_ref": "L-B",
                    "trail_level": "ADVANCED",
                },
                STAFF,
            )[0],
            201,
        )
        segments = [
            ("STAGING", "索道下站集结段", 0, 0),
            ("VALLEY-1", "临瀑谷道", 1, 1),
            ("VALLEY-2", "谷底环线", 2, 1),
            ("RIDGE-BYPASS", "山脊绕行段", 3, 0),
        ]
        for ref, name, position, in_valley in segments:
            self.assertEqual(
                self._post(
                    "/api/staff/segments",
                    {
                        "segment_ref": ref,
                        "route_ref": "TTZ-MAIN",
                        "display_name": name,
                        "position": position,
                        "in_valley": bool(in_valley),
                        "capacity": 30,
                    },
                    STAFF,
                )[0],
                201,
            )
        self.assertEqual(
            self._post(
                "/api/staff/plans",
                {
                    "team_ref": "TEAM-A",
                    "items": [
                        {"segment_ref": "STAGING", "window_start": "2026-09-19T08:00:00+08:00", "window_end": "2026-09-19T08:30:00+08:00"},
                        {"segment_ref": "VALLEY-1", "window_start": "2026-09-19T09:00:00+08:00", "window_end": "2026-09-19T10:30:00+08:00"},
                    ],
                },
                STAFF,
            )[0],
            201,
        )
        self.assertEqual(
            self._post(
                "/api/staff/plans",
                {
                    "team_ref": "TEAM-B",
                    "items": [
                        {"segment_ref": "STAGING", "window_start": "2026-09-19T07:00:00+08:00", "window_end": "2026-09-19T07:30:00+08:00"},
                        {"segment_ref": "VALLEY-1", "window_start": "2026-09-19T07:40:00+08:00", "window_end": "2026-09-19T09:30:00+08:00"},
                    ],
                },
                STAFF,
            )[0],
            201,
        )
        self.assertEqual(self._post("/api/staff/exits", {"exit_ref": "EAST-GATE", "display_name": "东门"}, STAFF)[0], 201)
        self.assertEqual(self._post("/api/staff/shuttles", {"shuttle_ref": "BUS-1", "display_name": "一号接驳车", "capacity": 20}, STAFF)[0], 201)
        self.assertEqual(
            self._post(
                "/api/staff/alternatives",
                {
                    "connection_ref": "ALT-RIDGE",
                    "from_segment_ref": "STAGING",
                    "to_segment_ref": "RIDGE-BYPASS",
                    "kind": "DETOUR",
                    "trail_level": "BEGINNER",
                    "note": "索道停运时的山脊绕行",
                },
                STAFF,
            )[0],
            201,
        )
        self.assertEqual(
            self._post(
                "/api/staff/evacuation-channels",
                {
                    "channel_ref": "EVAC-WATERFALL",
                    "from_segment_ref": "VALLEY-1",
                    "direction": "DOWNSTREAM",
                    "to_exit_ref": "EAST-GATE",
                },
                STAFF,
            )[0],
            201,
        )

    # ---- 基础与身份 -----------------------------------------------------

    def test_health(self) -> None:
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_staff_endpoints_require_role(self) -> None:
        status, body = self._post("/api/staff/ropeway-status", {"facility_ref": "ROPE-1", "status": "SUSPENDED"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "UNAUTHENTICATED")

    def test_visitors_only_for_own_leader(self) -> None:
        status, body = self.call("GET", "/api/teams/TEAM-A/visitors", {"X-Leader-Ref": "L-A"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["visitors"]), 2)

        status, body = self.call("GET", "/api/teams/TEAM-A/visitors", {"X-Leader-Ref": "L-B"})
        self.assertEqual(status, 403)

        status, _ = self.call("GET", "/api/teams/TEAM-A/visitors", STAFF)
        self.assertEqual(status, 401)

    # ---- 断网补报 -------------------------------------------------------

    def test_backfill_keeps_original_and_upload_time(self) -> None:
        # 现场 09:00 入谷，山区断网，11:00 才补报。
        status, body = self._post(
            "/api/teams/TEAM-A/checkins",
            {"segment_ref": "VALLEY-1", "action": "ENTER", "occurred_at": "2026-09-19T09:00:00+08:00"},
            {"X-Leader-Ref": "L-A"},
        )
        self.clock.advance(120)
        self.assertEqual(status, 201)
        self.assertTrue(body["is_backfill"])
        self.assertEqual(body["occurred_at"], "2026-09-19T09:00:00+08:00")
        self.assertEqual(body["uploaded_at"], T0.astimezone(timezone.utc).isoformat())

    def test_naive_timestamp_rejected(self) -> None:
        status, body = self._post(
            "/api/teams/TEAM-A/checkins",
            {"segment_ref": "VALLEY-1", "action": "ENTER", "occurred_at": "2026-09-19T09:00:00"},
            {"X-Leader-Ref": "L-A"},
        )
        self.assertEqual(status, 400)
        self.assertIn("时区偏移", body["message"])

    def test_occupancy_replays_by_original_time_not_upload_order(self) -> None:
        # 先补报一条更早的入谷，再实时记录离谷；重放按现场时间排序。
        self.clock.advance(180)
        self.assertEqual(
            self._post(
                "/api/teams/TEAM-A/checkins",
                {"segment_ref": "VALLEY-1", "action": "ENTER", "occurred_at": "2026-09-19T07:30:00+08:00"},
                {"X-Leader-Ref": "L-A"},
            )[0],
            201,
        )
        self.assertEqual(
            self._post(
                "/api/teams/TEAM-A/checkins",
                {"segment_ref": "VALLEY-1", "action": "LEAVE", "occurred_at": "2026-09-19T09:30:00+08:00"},
                {"X-Leader-Ref": "L-A"},
            )[0],
            201,
        )
        status, body = self.call("GET", "/api/occupancy", STAFF)
        self.assertEqual(status, 200)
        team_a = next(t for t in body["teams"] if t["team_ref"] == "TEAM-A")
        self.assertIsNone(team_a["current"])

    def test_occupancy_requires_identity(self) -> None:
        status, _ = self.call("GET", "/api/occupancy")
        self.assertEqual(status, 401)

    # ---- 索道停运后的分化处置 -------------------------------------------

    def _suspend_ropeway(self) -> None:
        self.assertEqual(
            self._post(
                "/api/staff/ropeway-status",
                {"facility_ref": "ROPE-1", "status": "SUSPENDED", "notice": "大风临时停运"},
                STAFF,
            )[0],
            201,
        )

    def test_team_not_in_valley_can_reroute(self) -> None:
        self._suspend_ropeway()
        # A 队尚在集结段（按计划起点判定，也可以先签到 STAGING）。
        status, body = self.call("GET", "/api/teams/TEAM-A/alternatives", {"X-Leader-Ref": "L-A"})
        self.assertEqual(status, 200)
        refs = [a["connection_ref"] for a in body["alternatives"]]
        self.assertIn("ALT-RIDGE", refs)

        status, body = self._post(
            "/api/teams/TEAM-A/alternatives",
            {"connection_ref": "ALT-RIDGE"},
            {"X-Leader-Ref": "L-A"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["decision"], "REROUTE")

        # 重复改线被拒。
        status, body = self._post(
            "/api/teams/TEAM-A/alternatives",
            {"connection_ref": "ALT-RIDGE"},
            {"X-Leader-Ref": "L-A"},
        )
        self.assertEqual(status, 409)

    def test_valley_team_cannot_reroute_but_can_record_actions(self) -> None:
        self._suspend_ropeway()
        self.assertEqual(
            self._post(
                "/api/teams/TEAM-B/checkins",
                {"segment_ref": "VALLEY-1", "action": "ENTER", "occurred_at": "2026-09-19T07:50:00+08:00"},
                {"X-Leader-Ref": "L-B"},
            )[0],
            201,
        )
        status, body = self.call("GET", "/api/teams/TEAM-B/alternatives", {"X-Leader-Ref": "L-B"})
        self.assertEqual(status, 409)

        status, body = self._post(
            "/api/teams/TEAM-B/alternatives",
            {"connection_ref": "ALT-RIDGE"},
            {"X-Leader-Ref": "L-B"},
        )
        self.assertEqual(status, 409)

        # 先等待，随后改为人工护送；旧决定被标记取代但保留。
        status, first = self._post(
            "/api/teams/TEAM-B/valley-decisions",
            {"decision": "WAIT", "note": "原地等待索道恢复"},
            {"X-Leader-Ref": "L-B"},
        )
        self.assertEqual(status, 201)
        self.clock.advance(30)
        status, body = self._post(
            "/api/teams/TEAM-B/valley-decisions",
            {"decision": "ESCORT", "note": "维护人员进谷护送"},
            {"X-Leader-Ref": "L-B"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["decision"], "ESCORT")
        self.assertNotEqual(first["id"], body["id"])

    def test_not_yet_entered_team_cannot_use_valley_decision(self) -> None:
        status, body = self._post(
            "/api/teams/TEAM-A/valley-decisions",
            {"decision": "WAIT"},
            {"X-Leader-Ref": "L-A"},
        )
        self.assertEqual(status, 409)

    def test_alternative_hidden_when_target_segment_closed(self) -> None:
        self.assertEqual(
            self._post(
                "/api/staff/closures",
                {"segment_ref": "RIDGE-BYPASS", "reason": "落石排查"},
                STAFF,
            )[0],
            201,
        )
        status, body = self.call("GET", "/api/teams/TEAM-A/alternatives", {"X-Leader-Ref": "L-A"})
        self.assertEqual(status, 200)
        self.assertEqual(body["alternatives"], [])

        # 解封后重新可见。
        status, _ = self._post("/api/staff/closures/1/lift", {}, STAFF)
        self.assertEqual(status, 200)
        status, body = self.call("GET", "/api/teams/TEAM-A/alternatives", {"X-Leader-Ref": "L-A"})
        self.assertEqual(
            [a["connection_ref"] for a in body["alternatives"]], ["ALT-RIDGE"]
        )

    def test_alternative_filtered_by_trail_level(self) -> None:
        # 高级队看不到只面向初级的绕行连接。
        status, body = self.call("GET", "/api/teams/TEAM-B/alternatives", {"X-Leader-Ref": "L-B"})
        self.assertEqual(status, 200)
        self.assertEqual(body["alternatives"], [])

    # ---- 出口与接驳车互斥 -----------------------------------------------

    def _window(self, start: str, end: str, team_headers, team_ref="TEAM-A", **extra):
        body = {"window_start": start, "window_end": end, **extra}
        return self._post(f"/api/teams/{team_ref}/reservations", body, team_headers)

    def test_exit_and_shuttle_cannot_be_double_booked(self) -> None:
        status, first = self._window(
            "2026-09-19T10:00:00+08:00",
            "2026-09-19T10:30:00+08:00",
            {"X-Leader-Ref": "L-A"},
            exit_ref="EAST-GATE",
            shuttle_ref="BUS-1",
        )
        self.assertEqual(status, 201)

        # 重叠时段：出口与车辆都被 A 队占用，B 队任一资源冲突都应被拒。
        status, body = self._window(
            "2026-09-19T10:15:00+08:00",
            "2026-09-19T10:45:00+08:00",
            {"X-Leader-Ref": "L-B"},
            team_ref="TEAM-B",
            exit_ref="EAST-GATE",
        )
        self.assertEqual(status, 409)
        self.assertIn("EAST-GATE", body["message"])

        status, body = self._window(
            "2026-09-19T10:15:00+08:00",
            "2026-09-19T10:45:00+08:00",
            {"X-Leader-Ref": "L-B"},
            team_ref="TEAM-B",
            shuttle_ref="BUS-1",
        )
        self.assertEqual(status, 409)
        self.assertIn("BUS-1", body["message"])

        # 不重叠时段可以使用同一出口。
        status, _ = self._window(
            "2026-09-19T10:30:00+08:00",
            "2026-09-19T11:00:00+08:00",
            {"X-Leader-Ref": "L-B"},
            team_ref="TEAM-B",
            exit_ref="EAST-GATE",
        )
        self.assertEqual(status, 201)

        # A 队释放后，重叠占用重新可用。
        status, _ = self._post(f"/api/reservations/{first['id']}/release", {}, {"X-Leader-Ref": "L-A"})
        self.assertEqual(status, 200)
        status, _ = self._window(
            "2026-09-19T10:00:00+08:00",
            "2026-09-19T10:30:00+08:00",
            {"X-Leader-Ref": "L-B"},
            team_ref="TEAM-B",
            shuttle_ref="BUS-1",
        )
        self.assertEqual(status, 201)

    def test_other_leader_cannot_release_reservation(self) -> None:
        status, first = self._window(
            "2026-09-19T10:00:00+08:00",
            "2026-09-19T10:30:00+08:00",
            {"X-Leader-Ref": "L-A"},
            exit_ref="EAST-GATE",
        )
        self.assertEqual(status, 201)
        status, _ = self._post(f"/api/reservations/{first['id']}/release", {}, {"X-Leader-Ref": "L-B"})
        self.assertEqual(status, 403)

    # ---- 复盘 -----------------------------------------------------------

    def test_review_explains_how_each_team_left(self) -> None:
        # A 队改线后离开。
        self.assertEqual(
            self._post(
                "/api/teams/TEAM-A/alternatives",
                {"connection_ref": "ALT-RIDGE"},
                {"X-Leader-Ref": "L-A"},
            )[0],
            201,
        )
        status, _ = self._post(
            "/api/teams/TEAM-A/outcomes",
            {
                "how": "REROUTED",
                "exit_ref": "EAST-GATE",
                "left_affected_at": "2026-09-19T11:20:00+08:00",
            },
            {"X-Leader-Ref": "L-A"},
        )
        self.assertEqual(status, 201)

        # B 队护送决定与结果必须一致。
        self.assertEqual(
            self._post(
                "/api/teams/TEAM-B/checkins",
                {"segment_ref": "VALLEY-1", "action": "ENTER", "occurred_at": "2026-09-19T07:50:00+08:00"},
                {"X-Leader-Ref": "L-B"},
            )[0],
            201,
        )
        self.assertEqual(
            self._post(
                "/api/teams/TEAM-B/valley-decisions",
                {"decision": "ESCORT"},
                {"X-Leader-Ref": "L-B"},
            )[0],
            201,
        )
        status, body = self._post(
            "/api/teams/TEAM-B/outcomes",
            {"how": "TURNED_BACK", "left_affected_at": "2026-09-19T12:00:00+08:00"},
            {"X-Leader-Ref": "L-B"},
        )
        self.assertEqual(status, 409)

        status, _ = self._post(
            "/api/teams/TEAM-B/outcomes",
            {
                "how": "ESCORTED",
                "exit_ref": "EAST-GATE",
                "shuttle_ref": "BUS-1",
                "left_affected_at": "2026-09-19T12:00:00+08:00",
                "note": "维护组护送出谷后乘接驳车",
            },
            {"X-Leader-Ref": "L-B"},
        )
        self.assertEqual(status, 201)

        status, body = self.call("GET", "/api/staff/review", STAFF)
        self.assertEqual(status, 200)
        hows = {row["team_ref"]: row["how"] for row in body["outcomes"]}
        self.assertEqual(hows, {"TEAM-A": "REROUTED", "TEAM-B": "ESCORTED"})
        self.assertEqual(body["teams_without_outcome"], [])

    def test_review_staff_only(self) -> None:
        status, _ = self.call("GET", "/api/staff/review", {"X-Leader-Ref": "L-A"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
