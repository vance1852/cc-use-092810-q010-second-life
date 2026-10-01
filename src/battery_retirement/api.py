"""无第三方依赖的退役评估与梯次利用 HTTP JSON 接口。"""

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
    """将 HTTP 路由映射到退役处置领域服务，便于无网络单元测试。"""

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
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}

            if method == "POST" and path == "/users":
                return Response(
                    201,
                    self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]),
                )

            actor = self._actor(normalized)

            if method == "POST" and path == "/components":
                return Response(201, self.service.register_component(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "components" and parts[2] == "config_revisions":
                return Response(201, self.service.add_config_revision(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "components" and parts[2] == "measurements":
                return Response(
                    201,
                    self.service.import_measurements(
                        actor, parts[1], payload.get("measurements", [])
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "components" and parts[2] == "quality_events":
                return Response(201, self.service.record_quality_event(actor, parts[1], payload))
            if method == "GET" and len(parts) == 3 and parts[0] == "components" and parts[2] == "disposition":
                return Response(200, self.service.disposition_report(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "components" and parts[2] == "reservations":
                return Response(200, self.service.reservation_history(actor, parts[1]))

            if method == "POST" and path == "/policies":
                return Response(201, self.service.publish_policy(actor, payload))

            if method == "POST" and path == "/assessments":
                return Response(201, self.service.open_assessment(
                    actor, payload["assessment_id"], payload["component_id"], int(payload["config_revision"]),
                    payload["policy_id"], int(payload["policy_version"]), payload["window_start"],
                    payload.get("window_cutoff"),
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "assessments":
                return Response(200, self.service.get_assessment(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "submit":
                return Response(200, self.service.submit_assessment(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "approve":
                return Response(200, self.service.approve_assessment(actor, parts[1], payload.get("note", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "reject":
                return Response(200, self.service.reject_assessment(actor, parts[1], payload.get("note", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "recompute":
                return Response(200, self.service.recompute(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "evidence_gaps":
                return Response(200, self.service.evidence_gaps(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "residual_value":
                return Response(200, self.service.residual_value(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "assessments" and parts[2] == "reviews":
                return Response(201, self.service.request_review(
                    actor, parts[1], payload["reason"], payload.get("new_evidence_refs", [])
                ))

            if method == "POST" and len(parts) == 3 and parts[0] == "reviews" and parts[2] == "decide":
                return Response(200, self.service.decide_review(
                    actor, int(parts[1]), bool(payload["accept"]), payload.get("note", "")
                ))

            if method == "GET" and path == "/reconciliation":
                return Response(200, self.service.reconciliation(actor))

            if method == "POST" and path == "/candidate_batches":
                return Response(201, self.service.create_candidate_batch(
                    actor, payload["batch_id"], payload.get("note")
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "candidate_batches":
                return Response(200, self.service.get_candidate_batch(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "candidate_batches" and parts[2] == "components":
                return Response(201, self.service.add_candidate_component(
                    actor, parts[1], payload["component_id"]
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "candidate_batches" and parts[2] == "seal":
                return Response(200, self.service.seal_candidate_batch(actor, parts[1]))

            if method == "POST" and path == "/reuse_projects":
                return Response(201, self.service.create_reuse_project(
                    actor, payload["project_id"], payload["name"]
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "reuse_projects" and parts[2] == "close":
                return Response(200, self.service.close_project(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "reuse_projects" and parts[2] == "fail":
                return Response(200, self.service.fail_project(actor, parts[1], payload.get("reason", "")))

            if method == "POST" and path == "/reservations":
                return Response(201, self.service.reserve_capacity(
                    actor, payload["reservation_id"], payload["project_id"],
                    payload["batch_id"], payload["component_id"],
                ))
            if method == "POST" and path == "/reservations/expire":
                return Response(200, self.service.expire_due_reservations(actor))
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "withdraw":
                return Response(200, self.service.withdraw_reservation(
                    actor, parts[1], payload.get("reason", "")
                ))

            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except RetirementError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BatteryRetirement/1"

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
    parser = argparse.ArgumentParser(description="启动储能电池退役评估与梯次利用管理 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("battery_retirement.sqlite3"))
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
