
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from .service import Service, ServiceError, STAFF_ROLE
from scripts.migrate import migrate


def _int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Application:
    """纯函数式请求分发，便于不监听端口直接测试。"""

    def __init__(self, service: Service | None = None) -> None:
        self.service = service or Service()

    def handle(
        self, method: str, path: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, Any]]:
        leader_ref = headers.get("X-Leader-Ref")
        staff_role = headers.get("X-Staff-Role")
        data = self._parse_body(body, method)
        try:
            if method == "GET" and path == "/health":
                return 200, {"status": "ok"}
            if method == "GET" and path == "/api/occupancy":
                return 200, self.service.occupancy_view(leader_ref, staff_role)

            # 维护人员接口
            if method == "POST" and path == "/api/staff/leaders":
                return 201, self.service.register_leader(staff_role, data)
            if method == "POST" and path == "/api/staff/teams":
                return 201, self.service.register_team(staff_role, data)
            if method == "POST" and path == "/api/staff/segments":
                return 201, self.service.register_segment(staff_role, data)
            if method == "POST" and path == "/api/staff/exits":
                return 201, self.service.register_exit(staff_role, data)
            if method == "POST" and path == "/api/staff/shuttles":
                return 201, self.service.register_shuttle(staff_role, data)
            if method == "POST" and path == "/api/staff/plans":
                return 201, self.service.register_plan(staff_role, data)
            if method == "POST" and path == "/api/staff/ropeway-status":
                return 201, self.service.publish_ropeway_status(staff_role, data)
            if method == "POST" and path == "/api/staff/closures":
                return 201, self.service.publish_closure(staff_role, data)
            match = re.fullmatch(r"/api/staff/closures/(\d+)/lift", path)
            if method == "POST" and match:
                return 200, self.service.lift_closure(staff_role, int(match.group(1)))
            if method == "POST" and path == "/api/staff/evacuation-channels":
                return 201, self.service.publish_evacuation_channel(staff_role, data)
            match = re.fullmatch(r"/api/staff/evacuation-channels/([^/]+)/state", path)
            if method == "POST" and match:
                channel_ref = unquote(match.group(1))
                if not isinstance(data.get("open"), bool):
                    raise ServiceError(400, "VALIDATION_ERROR", "open 必须是布尔值")
                return 200, self.service.set_evacuation_channel(
                    staff_role, channel_ref, data["open"]
                )
            if method == "POST" and path == "/api/staff/alternatives":
                return 201, self.service.publish_alternative(staff_role, data)
            if method == "GET" and path == "/api/staff/review":
                return 200, self.service.review_outcomes(staff_role)

            # 带队人员接口
            match = re.fullmatch(r"/api/teams/([^/]+)/checkins", path)
            if method == "POST" and match:
                team_ref = unquote(match.group(1))
                return 201, self.service.record_checkin(leader_ref, team_ref, data)
            match = re.fullmatch(r"/api/teams/([^/]+)/visitors", path)
            if method == "GET" and match:
                team_ref = unquote(match.group(1))
                return 200, self.service.list_visitors(leader_ref, team_ref)
            match = re.fullmatch(r"/api/teams/([^/]+)/alternatives", path)
            if method == "GET" and match:
                team_ref = unquote(match.group(1))
                return 200, self.service.list_alternatives(leader_ref, team_ref)
            if method == "POST" and match:
                team_ref = unquote(match.group(1))
                return 201, self.service.decide_reroute(leader_ref, team_ref, data)
            match = re.fullmatch(r"/api/teams/([^/]+)/valley-decisions", path)
            if method == "POST" and match:
                team_ref = unquote(match.group(1))
                return 201, self.service.decide_valley_action(leader_ref, team_ref, data)
            match = re.fullmatch(r"/api/teams/([^/]+)/reservations", path)
            if method == "POST" and match:
                team_ref = unquote(match.group(1))
                return 201, self.service.claim_reservation(leader_ref, team_ref, data)
            match = re.fullmatch(r"/api/teams/([^/]+)/outcomes", path)
            if method == "POST" and match:
                team_ref = unquote(match.group(1))
                return 201, self.service.record_outcome(leader_ref, team_ref, data)
            match = re.fullmatch(r"/api/reservations/(\d+)/release", path)
            if method == "POST" and match:
                return 200, self.service.release_reservation(leader_ref, int(match.group(1)))

            return 404, {"error": "NOT_FOUND", "message": f"未找到路径：{method} {path}"}
        except ServiceError as exc:
            return exc.status, {"error": exc.code, "message": exc.message}

    @staticmethod
    def _parse_body(body: bytes | None, method: str) -> dict[str, Any]:
        if method in ("GET", "DELETE") or not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(400, "VALIDATION_ERROR", f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ServiceError(400, "VALIDATION_ERROR", "请求体必须是 JSON 对象")
        return data


class Handler(BaseHTTPRequestHandler):
    app = Application()

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        path = urlparse(self.path).path
        headers = {key: value for key, value in self.headers.items()}
        status, payload = self.app.handle(method, path, headers, body)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    migrate()
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()


# 便于其它模块引用维护角色常量。
__all__ = ["Application", "Handler", "STAFF_ROLE", "main"]
