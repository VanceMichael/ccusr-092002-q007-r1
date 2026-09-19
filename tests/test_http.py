"""HTTP 接口冒烟测试：真实启动服务，走完整请求周期。"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer

from app import db
from app.main import Handler


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fd, cls.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(cls.db_path)
        os.environ["DATABASE_PATH"] = cls.db_path
        db.init_db(cls.db_path)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        os.unlink(cls.db_path)

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps(payload) if payload is not None else None
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_full_flow_over_http(self) -> None:
        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)

        status, _ = self.request("PUT", "/segments/S1", {
            "route_ref": "ROUTE-MID", "level": "intermediate", "seq": 1})
        self.assertEqual(status, 200)
        status, _ = self.request("PUT", "/exits/EXIT-GATE",
                                 {"route_ref": "ROUTE-MID"})
        self.assertEqual(status, 200)
        status, _ = self.request("PUT", "/connections/C0", {
            "from_ref": "EXIT-GATE", "to_ref": "S1", "kind": "trail"})
        self.assertEqual(status, 200)

        status, team = self.request("POST", "/teams", {
            "team_ref": "TEAM-HTTP", "leader_ref": "LEADER-H",
            "route_ref": "ROUTE-MID", "level": "intermediate"})
        self.assertEqual(status, 201)
        self.assertEqual(team["team_ref"], "TEAM-HTTP")

        # 名单鉴权：无头部或非本人 → 403；本人 → 200。
        status, _ = self.request("PUT", "/teams/TEAM-HTTP/members",
                                 {"members": [{"member_ref": "M-1",
                                               "display_name": "游客"}]})
        self.assertEqual(status, 403)
        status, _ = self.request(
            "PUT", "/teams/TEAM-HTTP/members",
            {"members": [{"member_ref": "M-1", "display_name": "游客"}]},
            headers={"X-Leader-Ref": "LEADER-H"})
        self.assertEqual(status, 200)
        status, roster = self.request("GET", "/teams/TEAM-HTTP/members",
                                      headers={"X-Leader-Ref": "LEADER-H"})
        self.assertEqual(status, 200)
        self.assertEqual(roster["members"][0]["member_ref"], "M-1")

        # 断网补报：原始时间早于上传时间，source 为 backfill。
        status, checkin = self.request("POST", "/teams/TEAM-HTTP/checkins", {
            "segment_ref": "S1", "event": "arrive",
            "occurred_at": "2026-09-19T06:30:00+08:00"})
        self.assertEqual(status, 201)
        self.assertEqual(checkin["source"], "backfill")
        self.assertLess(datetime.fromisoformat(checkin["occurred_at"]),
                        datetime.fromisoformat(checkin["uploaded_at"]))

        # 封闭连接后评估：TEAM-HTTP 位于 S1，仍可改线。
        status, notice = self.request("POST", "/notices", {
            "notice_ref": "N-HTTP", "target_kind": "connection",
            "target_ref": "C0", "change": "closed",
            "effective_from": "2026-09-19T10:00:00+08:00",
            "published_by": "MAINT-1"})
        self.assertEqual(status, 201)
        evaluation = notice["evaluations"][0]
        self.assertEqual(evaluation["team_ref"], "TEAM-HTTP")
        self.assertEqual(evaluation["classification"], "in_affected_zone")

        # 受困队伍登记决定；不在受困区域的队伍登记会被拒绝。
        status, decision = self.request(
            "POST", "/teams/TEAM-HTTP/decisions",
            {"decision": "wait", "decided_by": "LEADER-H"})
        self.assertEqual(status, 201)
        self.assertEqual(decision["decision"], "wait")

        # 复盘可说明该队伍状态。
        status, report = self.request("GET", "/review?notice_ref=N-HTTP")
        self.assertEqual(status, 200)
        self.assertEqual(report["teams"][0]["exit_means"], "wait")
        self.assertIn("等待", report["teams"][0]["exit_account"])

        # 未知路径与非法 JSON。
        status, _ = self.request("GET", "/no-such-path")
        self.assertEqual(status, 404)
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        connection.request("POST", "/teams", body="{not json",
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 400)
        connection.close()


if __name__ == "__main__":
    unittest.main()
