"""分段行程接续服务 HTTP 接口。

运行参数：PORT 指定监听端口，DATABASE_PATH 指定 SQLite 数据文件。
带队人员身份通过 X-Leader-Ref 请求头与登记信息比对（未接入统一认证）。
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app import db, domain


def _json_response(handler, status: int, payload) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _health(conn, match, query, body, headers):
    return 200, {"status": "ok"}


def _put_segment(conn, match, query, body, headers):
    return 200, domain.upsert_segment(conn, match.group("ref"), body)


def _put_exit(conn, match, query, body, headers):
    return 200, domain.upsert_exit(conn, match.group("ref"), body)


def _put_connection(conn, match, query, body, headers):
    return 200, domain.upsert_connection(conn, match.group("ref"), body)


def _post_team(conn, match, query, body, headers):
    return 201, domain.register_team(conn, body)


def _put_members(conn, match, query, body, headers):
    return 200, domain.replace_members(
        conn, match.group("ref"), headers.get("X-Leader-Ref"), body)


def _get_members(conn, match, query, body, headers):
    return 200, domain.list_members(
        conn, match.group("ref"), headers.get("X-Leader-Ref"))


def _post_checkin(conn, match, query, body, headers):
    return 201, domain.record_checkin(conn, match.group("ref"), body)


def _get_checkins(conn, match, query, body, headers):
    return 200, domain.list_checkins(conn, match.group("ref"))


def _get_options(conn, match, query, body, headers):
    return 200, domain.team_options(conn, match.group("ref"))


def _post_decision(conn, match, query, body, headers):
    return 201, domain.record_decision(conn, match.group("ref"), body)


def _respond_offer(conn, match, query, body, headers):
    return 200, domain.respond_offer(conn, match.group("ref"), body)


def _post_notice(conn, match, query, body, headers):
    return 201, domain.publish_notice(conn, body)


def _post_booking(conn, match, query, body, headers):
    return 201, domain.create_booking(conn, body)


def _release_booking(conn, match, query, body, headers):
    return 200, domain.release_booking(conn, match.group("ref"))


def _get_occupancy(conn, match, query, body, headers):
    return 200, domain.occupancy(conn)


def _get_review(conn, match, query, body, headers):
    notice_ref = query.get("notice_ref", [None])[0]
    return 200, domain.review(conn, notice_ref)


ROUTES = [
    ("GET", re.compile(r"^/health$"), _health),
    ("PUT", re.compile(r"^/segments/(?P<ref>[^/]+)$"), _put_segment),
    ("PUT", re.compile(r"^/exits/(?P<ref>[^/]+)$"), _put_exit),
    ("PUT", re.compile(r"^/connections/(?P<ref>[^/]+)$"), _put_connection),
    ("POST", re.compile(r"^/teams$"), _post_team),
    ("PUT", re.compile(r"^/teams/(?P<ref>[^/]+)/members$"), _put_members),
    ("GET", re.compile(r"^/teams/(?P<ref>[^/]+)/members$"), _get_members),
    ("POST", re.compile(r"^/teams/(?P<ref>[^/]+)/checkins$"), _post_checkin),
    ("GET", re.compile(r"^/teams/(?P<ref>[^/]+)/checkins$"), _get_checkins),
    ("GET", re.compile(r"^/teams/(?P<ref>[^/]+)/options$"), _get_options),
    ("POST", re.compile(r"^/teams/(?P<ref>[^/]+)/decisions$"), _post_decision),
    ("POST", re.compile(r"^/offers/(?P<ref>[^/]+)/respond$"), _respond_offer),
    ("POST", re.compile(r"^/notices$"), _post_notice),
    ("POST", re.compile(r"^/bookings$"), _post_booking),
    ("POST", re.compile(r"^/bookings/(?P<ref>[^/]+)/release$"), _release_booking),
    ("GET", re.compile(r"^/occupancy$"), _get_occupancy),
    ("GET", re.compile(r"^/review$"), _get_review),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        body = None
        if method in ("POST", "PUT"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "bad_request",
                                           "detail": "请求体不是合法 JSON"})
                return
        connection = db.connect()
        try:
            for route_method, pattern, handler in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(parsed.path)
                if match:
                    try:
                        status, payload = handler(
                            connection, match, query, body, self.headers)
                        connection.commit()
                    except domain.DomainError as exc:
                        connection.rollback()
                        status, payload = exc.status, {
                            "error": exc.code, "detail": exc.detail}
                    except Exception:
                        connection.rollback()
                        raise
                    _json_response(self, status, payload)
                    return
            _json_response(self, 404, {"error": "not_found", "detail": "接口不存在"})
        finally:
            connection.close()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    db.init_db()
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
