"""分段行程接续的领域场景测试。

场景：天堂寨中级步道 ROUTE-MID，路段 S1-S2-S3 依次相连，S3 经索道 CABLE
通往谷底 S4；S4 唯一对外通道即索道。EV1 为 S2 通往东出口的疏散通道，
初始封闭。索道临时停运后：
- 尚未出发的 TEAM-A 仍可改线；
- 停留在 S2 的 TEAM-B 仍能安全改线，获得替代连接建议；
- 已入谷的 TEAM-C 只能登记等待 / 折返 / 人工护送决定。
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app import db, domain

TZ = timezone(timedelta(hours=8))


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class ItineraryTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.path)
        db.init_db(self.path)
        self.conn = db.connect(self.path)
        self._seed_graph()
        self._seed_teams()

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.path)

    # ------------------------------------------------------------ 数据准备

    def _seed_graph(self) -> None:
        for seq, ref in enumerate(("S1", "S2", "S3", "S4"), start=1):
            domain.upsert_segment(self.conn, ref, {
                "route_ref": "ROUTE-MID", "level": "intermediate", "seq": seq})
        for ref in ("EXIT-GATE", "EXIT-EAST"):
            domain.upsert_exit(self.conn, ref, {"route_ref": "ROUTE-MID"})
        domain.upsert_connection(self.conn, "C0", {
            "from_ref": "EXIT-GATE", "to_ref": "S1", "kind": "trail"})
        domain.upsert_connection(self.conn, "C1", {
            "from_ref": "S1", "to_ref": "S2", "kind": "trail"})
        domain.upsert_connection(self.conn, "C2", {
            "from_ref": "S2", "to_ref": "S3", "kind": "trail"})
        domain.upsert_connection(self.conn, "CABLE", {
            "from_ref": "S3", "to_ref": "S4", "kind": "cableway"})
        domain.upsert_connection(self.conn, "EV1", {
            "from_ref": "S2", "to_ref": "EXIT-EAST", "kind": "evacuation"})
        # 疏散通道初始封闭。
        self.conn.execute(
            "UPDATE connections SET status='closed' WHERE conn_ref='EV1'")
        self.conn.commit()

    def _seed_teams(self) -> None:
        for team_ref, leader_ref in (("TEAM-A", "LEADER-A"),
                                     ("TEAM-B", "LEADER-B"),
                                     ("TEAM-C", "LEADER-C")):
            domain.register_team(self.conn, {
                "team_ref": team_ref, "leader_ref": leader_ref,
                "route_ref": "ROUTE-MID", "level": "intermediate"})
        self.conn.commit()

    def _close_cableway(self) -> dict:
        result = domain.publish_notice(self.conn, {
            "notice_ref": "N1", "target_kind": "connection", "target_ref": "CABLE",
            "change": "closed", "effective_from": "2026-09-19T10:00:00+08:00",
            "published_by": "MAINT-1"})
        self.conn.commit()
        return result

    def _evaluation(self, result, team_ref) -> dict:
        return next(e for e in result["evaluations"] if e["team_ref"] == team_ref)

    # ------------------------------------------------------------ 签到与补报

    def test_checkin_backfill_keeps_original_and_upload_time(self) -> None:
        now = datetime.now(TZ)
        late = domain.record_checkin(self.conn, "TEAM-C", {
            "segment_ref": "S4", "event": "arrive",
            "occurred_at": iso(now - timedelta(hours=2))})
        fresh = domain.record_checkin(self.conn, "TEAM-B", {
            "segment_ref": "S2", "event": "arrive",
            "occurred_at": iso(now - timedelta(minutes=1))})
        self.assertEqual(late["source"], "backfill")
        self.assertEqual(fresh["source"], "live")
        for record in (late, fresh):
            self.assertIn("occurred_at", record)
            self.assertIn("uploaded_at", record)
        self.assertLess(domain.parse_time(late["occurred_at"], "occurred_at"),
                        domain.parse_time(late["uploaded_at"], "uploaded_at"))

        stored = domain.list_checkins(self.conn, "TEAM-C")["checkins"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["source"], "backfill")
        self.assertEqual(stored[0]["occurred_at"], late["occurred_at"])

    def test_occupancy_reflects_latest_position(self) -> None:
        now = datetime.now(TZ)
        domain.record_checkin(self.conn, "TEAM-B", {
            "segment_ref": "S2", "event": "arrive", "occurred_at": iso(now)})
        domain.record_checkin(self.conn, "TEAM-C", {
            "segment_ref": "S4", "event": "arrive", "occurred_at": iso(now)})
        board = {row["segment_ref"]: row["teams"]
                 for row in domain.occupancy(self.conn)["segments"]}
        self.assertEqual(board, {"S2": ["TEAM-B"], "S4": ["TEAM-C"]})

        domain.record_checkin(self.conn, "TEAM-B", {
            "segment_ref": "S2", "event": "leave",
            "occurred_at": iso(now + timedelta(minutes=5))})
        board = {row["segment_ref"]: row["teams"]
                 for row in domain.occupancy(self.conn)["segments"]}
        self.assertEqual(board, {"S4": ["TEAM-C"]})

    # ------------------------------------------------------------ 名单访问

    def test_roster_only_visible_to_owning_leader(self) -> None:
        payload = {"members": [
            {"member_ref": "M-01", "display_name": "游客甲"},
            {"member_ref": "M-02", "display_name": "游客乙", "note": "需轮椅协助"}]}
        saved = domain.replace_members(self.conn, "TEAM-A", "LEADER-A", payload)
        self.assertEqual(len(saved["members"]), 2)

        listed = domain.list_members(self.conn, "TEAM-A", "LEADER-A")
        self.assertEqual([m["member_ref"] for m in listed["members"]], ["M-01", "M-02"])

        with self.assertRaises(domain.Forbidden):
            domain.list_members(self.conn, "TEAM-A", "LEADER-B")
        with self.assertRaises(domain.Forbidden):
            domain.list_members(self.conn, "TEAM-A", None)
        with self.assertRaises(domain.Forbidden):
            domain.replace_members(self.conn, "TEAM-A", "LEADER-B", payload)

    # ------------------------------------------------------------ 通告与分类

    def test_cableway_closure_classifies_teams(self) -> None:
        now = datetime.now(TZ)
        domain.record_checkin(self.conn, "TEAM-B", {
            "segment_ref": "S2", "event": "arrive", "occurred_at": iso(now)})
        domain.record_checkin(self.conn, "TEAM-C", {
            "segment_ref": "S4", "event": "arrive", "occurred_at": iso(now)})
        self.conn.commit()

        result = self._close_cableway()
        team_a = self._evaluation(result, "TEAM-A")
        team_b = self._evaluation(result, "TEAM-B")
        team_c = self._evaluation(result, "TEAM-C")

        self.assertEqual(team_a["classification"], "not_started")
        self.assertEqual(team_b["classification"], "can_reroute")
        self.assertEqual(team_c["classification"], "in_affected_zone")

        # 只对仍能安全改线的队伍提供替代连接。
        self.assertTrue(team_a["offers"])
        self.assertEqual({o["conn_ref"] for o in team_b["offers"]}, {"C1", "C2"})
        self.assertEqual(team_c["offers"], [])
        self.assertEqual(team_c["decision_choices"], ["wait", "retreat", "escort"])

    def test_decision_only_allowed_for_trapped_team(self) -> None:
        now = datetime.now(TZ)
        domain.record_checkin(self.conn, "TEAM-B", {
            "segment_ref": "S2", "event": "arrive", "occurred_at": iso(now)})
        domain.record_checkin(self.conn, "TEAM-C", {
            "segment_ref": "S4", "event": "arrive", "occurred_at": iso(now)})
        self.conn.commit()
        self._close_cableway()

        saved = domain.record_decision(self.conn, "TEAM-C", {
            "decision": "escort", "decided_by": "LEADER-C",
            "note": "由维护人员沿检修道护送"})
        self.assertEqual(saved["decision"], "escort")
        self.assertEqual(saved["notice_ref"], "N1")

        with self.assertRaises(domain.Conflict):
            domain.record_decision(self.conn, "TEAM-B", {
                "decision": "wait", "decided_by": "LEADER-B"})
        with self.assertRaises(domain.BadRequest):
            domain.record_decision(self.conn, "TEAM-C", {
                "decision": "fly", "decided_by": "LEADER-C"})

    def test_evacuation_channel_reopen_refreshes_offers(self) -> None:
        now = datetime.now(TZ)
        domain.record_checkin(self.conn, "TEAM-B", {
            "segment_ref": "S2", "event": "arrive", "occurred_at": iso(now)})
        domain.record_checkin(self.conn, "TEAM-C", {
            "segment_ref": "S4", "event": "arrive", "occurred_at": iso(now)})
        self.conn.commit()
        self._close_cableway()

        reopened = domain.publish_notice(self.conn, {
            "notice_ref": "N2", "target_kind": "connection", "target_ref": "EV1",
            "change": "open", "effective_from": "2026-09-19T11:00:00+08:00",
            "published_by": "MAINT-1"})
        self.conn.commit()
        team_b = self._evaluation(reopened, "TEAM-B")
        self.assertEqual({o["conn_ref"] for o in team_b["offers"]}, {"C1", "C2", "EV1"})
        # S4 依旧孤立，TEAM-C 仍受困。
        self.assertEqual(self._evaluation(reopened, "TEAM-C")["classification"],
                         "in_affected_zone")

        offer_ev1 = next(o for o in team_b["offers"] if o["conn_ref"] == "EV1")
        accepted = domain.respond_offer(self.conn, offer_ev1["offer_ref"],
                                        {"action": "accept"})
        self.assertEqual(accepted["status"], "accepted")
        options = domain.team_options(self.conn, "TEAM-B")
        self.assertEqual(options["offers"], [])  # 其余建议已被取代
        with self.assertRaises(domain.Conflict):
            domain.respond_offer(self.conn, offer_ev1["offer_ref"],
                                 {"action": "accept"})

    # ------------------------------------------------------------ 接驳占用

    def _book(self, team_ref, exit_ref, vehicle_ref, start, end):
        return domain.create_booking(self.conn, {
            "team_ref": team_ref, "exit_ref": exit_ref, "vehicle_ref": vehicle_ref,
            "window_start": start, "window_end": end})

    def test_exit_and_vehicle_cannot_be_double_booked(self) -> None:
        first = self._book("TEAM-B", "EXIT-EAST", "VEH-1",
                           "2026-09-19T14:00:00+08:00", "2026-09-19T15:00:00+08:00")
        self.assertEqual(first["status"], "active")

        # 同一出口时段重叠 → 冲突。
        with self.assertRaises(domain.Conflict):
            self._book("TEAM-C", "EXIT-EAST", "VEH-2",
                       "2026-09-19T14:30:00+08:00", "2026-09-19T15:30:00+08:00")
        # 同一车辆时段重叠（出口不同）→ 冲突。
        with self.assertRaises(domain.Conflict):
            self._book("TEAM-A", "EXIT-GATE", "VEH-1",
                       "2026-09-19T14:30:00+08:00", "2026-09-19T15:30:00+08:00")
        # 首尾相接不算重叠 → 允许。
        self._book("TEAM-C", "EXIT-EAST", "VEH-2",
                   "2026-09-19T15:00:00+08:00", "2026-09-19T16:00:00+08:00")
        # 不同出口不同车辆 → 允许。
        other = self._book("TEAM-A", "EXIT-GATE", "VEH-3",
                           "2026-09-19T14:30:00+08:00", "2026-09-19T15:30:00+08:00")

        # 释放后资源可再被占用。
        domain.release_booking(self.conn, other["booking_ref"])
        self._book("TEAM-C", "EXIT-GATE", "VEH-3",
                   "2026-09-19T14:45:00+08:00", "2026-09-19T15:15:00+08:00")
        with self.assertRaises(domain.Conflict):
            domain.release_booking(self.conn, other["booking_ref"])

    def test_booking_window_must_be_valid(self) -> None:
        with self.assertRaises(domain.BadRequest):
            self._book("TEAM-B", "EXIT-EAST", "VEH-1",
                       "2026-09-19T15:00:00+08:00", "2026-09-19T14:00:00+08:00")
        with self.assertRaises(domain.BadRequest):
            self._book("TEAM-B", "EXIT-EAST", "VEH-1",
                       "2026-09-19 14:00", "2026-09-19T15:00:00+08:00")

    # ------------------------------------------------------------ 复盘

    def test_review_explains_how_each_team_left(self) -> None:
        now = datetime.now(TZ)
        domain.record_checkin(self.conn, "TEAM-B", {
            "segment_ref": "S2", "event": "arrive", "occurred_at": iso(now)})
        domain.record_checkin(self.conn, "TEAM-C", {
            "segment_ref": "S4", "event": "arrive", "occurred_at": iso(now)})
        self.conn.commit()
        self._close_cableway()
        domain.record_decision(self.conn, "TEAM-C", {
            "decision": "escort", "decided_by": "LEADER-C"})
        reopened = domain.publish_notice(self.conn, {
            "notice_ref": "N2", "target_kind": "connection", "target_ref": "EV1",
            "change": "open", "effective_from": "2026-09-19T11:00:00+08:00",
            "published_by": "MAINT-1"})
        self.conn.commit()
        team_b = self._evaluation(reopened, "TEAM-B")
        offer_ev1 = next(o for o in team_b["offers"] if o["conn_ref"] == "EV1")
        domain.respond_offer(self.conn, offer_ev1["offer_ref"], {"action": "accept"})

        self._book("TEAM-B", "EXIT-EAST", "VEH-1",
                   "2026-09-19T14:00:00+08:00", "2026-09-19T15:00:00+08:00")
        self._book("TEAM-C", "EXIT-EAST", "VEH-2",
                   "2026-09-19T15:00:00+08:00", "2026-09-19T16:00:00+08:00")
        self._book("TEAM-A", "EXIT-GATE", "VEH-3",
                   "2026-09-19T14:30:00+08:00", "2026-09-19T15:30:00+08:00")

        report = domain.review(self.conn, "N2")
        self.assertEqual(report["trapped_zone"], ["S4"])
        teams = {row["team_ref"]: row for row in report["teams"]}

        self.assertEqual(teams["TEAM-B"]["exit_means"], "reroute")
        self.assertIn("EV1", teams["TEAM-B"]["exit_account"])
        self.assertIn("EXIT-EAST", teams["TEAM-B"]["exit_account"])
        self.assertIn("VEH-1", teams["TEAM-B"]["exit_account"])

        self.assertEqual(teams["TEAM-C"]["classification"], "in_affected_zone")
        self.assertEqual(teams["TEAM-C"]["exit_means"], "escort")
        self.assertIn("人工护送", teams["TEAM-C"]["exit_account"])
        self.assertIn("VEH-2", teams["TEAM-C"]["exit_account"])

        self.assertEqual(teams["TEAM-A"]["classification"], "not_started")
        self.assertEqual(teams["TEAM-A"]["exit_means"], "departed")
        self.assertIn("EXIT-GATE", teams["TEAM-A"]["exit_account"])

    def test_review_marks_unresolved_team(self) -> None:
        now = datetime.now(TZ)
        domain.record_checkin(self.conn, "TEAM-C", {
            "segment_ref": "S4", "event": "arrive", "occurred_at": iso(now)})
        self.conn.commit()
        self._close_cableway()
        report = domain.review(self.conn, "N1")
        teams = {row["team_ref"]: row for row in report["teams"]}
        self.assertEqual(teams["TEAM-C"]["exit_means"], "unresolved")
        self.assertEqual(teams["TEAM-C"]["exit_account"], "尚未记录离场方式")


if __name__ == "__main__":
    unittest.main()
