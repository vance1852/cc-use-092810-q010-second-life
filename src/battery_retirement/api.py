"""退役评估与梯次利用管理的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import RetirementError, ValidationFailed
from .service import RetirementService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: RetirementService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "battery-retirement"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            # 用户创建是引导接口，无需既有身份；其余操作（含需要鉴权的查询）都要求 X-Actor-Id。
            if not (method == "POST" and path == "/users"):
                actor = self._actor(normalized)
            else:
                actor = ""

            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                ))
            if method == "POST" and path == "/policies":
                return Response(201, self.service.publish_policy(actor, payload))
            if method == "POST" and path == "/components":
                return Response(201, self.service.register_component(actor, payload))
            if method == "POST" and path == "/measurements":
                records = payload.get("records")
                if not isinstance(records, list):
                    raise ValidationFailed("records 必须是数组")
                return Response(201, self.service.record_measurements(actor, records))

            if method == "POST" and path == "/assessments":
                return Response(201, self.service.prepare_assessment(
                    actor,
                    payload["assessment_id"],
                    payload["component_id"],
                    payload["policy_id"],
                    int(payload["policy_version"]),
                    payload["windows"],
                    payload.get("source_review_id"),
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "assessments":
                return Response(200, self.service.assessment(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "decision":
                return Response(200, self.service.decide_assessment(
                    actor, parts[1], bool(payload["approve"]), payload.get("note", "")
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "reviews":
                return Response(201, self.service.request_review(
                    actor, parts[1], payload["reason"], payload.get("new_windows")
                ))
            if method == "GET" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "recompute":
                return Response(200, self.service.recompute_assessment(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "preview":
                return Response(200, self.service.preview(
                    actor, parts[1], payload["policy_id"], int(payload["policy_version"]), payload["windows"]
                ))

            if method == "POST" and len(parts) == 3 and parts[0] == "reviews" and parts[2] == "decision":
                return Response(200, self.service.decide_review(
                    actor, int(parts[1]), bool(payload["accept"]), payload.get("note", "")
                ))

            if method == "GET" and len(parts) == 3 and parts[0] == "components" and parts[2] == "versions":
                return Response(200, self.service.list_versions(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "components" and parts[2] == "disposition":
                return Response(200, self.service.component_disposition(parts[1]))

            if method == "POST" and path == "/cascade/batches":
                return Response(201, self.service.create_cascade_batch(
                    actor, payload["batch_id"], payload.get("note", "")
                ))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["cascade", "batches"]:
                return Response(200, self.service.cascade_batch(parts[2]))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["cascade", "batches"] and parts[3] == "items":
                return Response(201, self.service.add_to_cascade_batch(
                    actor, parts[2], payload["component_id"]
                ))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["cascade", "batches"] and parts[3] == "seal":
                return Response(200, self.service.seal_cascade_batch(actor, parts[2]))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["cascade", "batches"] and parts[3] == "withdraw":
                return Response(200, self.service.withdraw_cascade_batch(
                    actor, parts[2], payload.get("reason", "")
                ))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["cascade", "batches"] and parts[3] == "fail":
                return Response(200, self.service.fail_cascade_batch(
                    actor, parts[2], payload.get("reason", "")
                ))

            if method == "POST" and path == "/projects":
                return Response(201, self.service.reserve_project(
                    actor,
                    payload["project_id"],
                    payload["batch_id"],
                    payload["requested_capacity_kwh"],
                    int(payload.get("hold_days", 30)),
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "projects":
                return Response(200, self.service.project(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "withdraw":
                return Response(200, self.service.withdraw_project(actor, parts[1], payload.get("reason", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "fail":
                return Response(200, self.service.mark_project_failed(actor, parts[1], payload.get("reason", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "confirm":
                return Response(200, self.service.confirm_project(
                    actor, parts[1], payload.get("reference", "")
                ))
            if method == "POST" and path == "/holds/expire":
                return Response(200, self.service.expire_holds(actor))

            if method == "POST" and path == "/dispositions":
                return Response(201, self.service.confirm_disposition(
                    actor, payload["component_id"], payload["final_destination"], payload.get("reference", "")
                ))
            if method == "GET" and path == "/dispositions/register":
                return Response(200, self.service.disposition_register(actor))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except RetirementError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RetirementBoard/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动退役评估与梯次利用管理 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("battery-retirement.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(JsonApplication(RetirementService(connection)))
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
