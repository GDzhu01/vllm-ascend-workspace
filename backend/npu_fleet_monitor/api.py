from __future__ import annotations

import json
import mimetypes
import re
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .db import Database
from .scheduler import AdaptiveScheduler
from .settings import Settings
from .workspace_adapter import WorkspaceDeviceAdapter


RANGES = {
    "1h": (3600, 60), "6h": (21600, 300), "24h": (86400, 600),
    "7d": (604800, 3600), "30d": (2592000, 14400), "90d": (7776000, 43200),
}


class App:
    def __init__(self, settings: Settings, db: Database, adapter: WorkspaceDeviceAdapter, scheduler: AdaptiveScheduler) -> None:
        self.settings = settings
        self.db = db
        self.adapter = adapter
        self.scheduler = scheduler
        self.web_root = settings.project_root / "dist" / "client"

    def overview(self) -> dict[str, Any]:
        servers = self.db.list_servers()
        snapshots = self.scheduler.snapshots()
        rows = []
        totals = {
            "servers": len(servers), "online_servers": 0, "npu_count": 0,
            "busy_npu_count": 0, "hbm_used_mb": 0, "hbm_total_mb": 0,
            "npu_util_percent": None,
        }
        utils: list[float] = []
        for server in servers:
            snapshot = snapshots.get(server["id"])
            status = snapshot.get("status") if snapshot else ("offline" if server.get("last_error") else "pending")
            row = {**server, "status": status, "snapshot": snapshot}
            rows.append(row)
            if status == "online" and snapshot:
                totals["online_servers"] += 1
                summary = snapshot.get("summary", {})
                totals["npu_count"] += summary.get("npu_count") or 0
                totals["busy_npu_count"] += summary.get("busy_npu_count") or 0
                totals["hbm_used_mb"] += summary.get("hbm_used_mb") or 0
                totals["hbm_total_mb"] += summary.get("hbm_total_mb") or 0
                if summary.get("npu_util_percent") is not None:
                    utils.append(float(summary["npu_util_percent"]))
        totals["npu_util_percent"] = round(sum(utils) / len(utils), 1) if utils else None
        totals["idle_npu_count"] = totals["npu_count"] - totals["busy_npu_count"]
        return {"generated_at": int(time.time()), "totals": totals, "servers": rows, "runtime": self.scheduler.runtime_state()}


class Handler(BaseHTTPRequestHandler):
    server_version = "NPUFleetMonitor/0.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/viewers/"):
            return
        super().log_message(fmt, *args)

    def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "public, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        origin = self.headers.get("Origin", "")
        if re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def json_response(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 1024 * 1024:
            raise ValueError("请求体为空或超过 1 MB")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("请求体不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.json_response({"status": "ok", "version": "0.1.0", "runtime": self.app.scheduler.runtime_state()})
        if parsed.path == "/api/overview":
            return self.json_response(self.app.overview())
        if parsed.path == "/api/servers":
            return self.json_response({"servers": self.app.db.list_servers()})
        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            range_name = query.get("range", ["24h"])[0]
            seconds, bucket = RANGES.get(range_name, RANGES["24h"])
            server_id = query.get("server_id", [None])[0]
            return self.json_response({
                "range": range_name, "bucket_seconds": bucket,
                "points": self.app.db.history(server_id, int(time.time()) - seconds, bucket),
            })
        if parsed.path == "/api/history/heatmap":
            query = parse_qs(parsed.query)
            range_name = query.get("range", ["7d"])[0]
            seconds, _ = RANGES.get(range_name, RANGES["7d"])
            server_id = query.get("server_id", [None])[0]
            if not server_id:
                return self.json_response({"error": "server_id is required"}, HTTPStatus.BAD_REQUEST)
            try:
                timezone_offset = int(query.get("timezone_offset", ["0"])[0])
            except ValueError:
                return self.json_response({"error": "timezone_offset must be an integer"}, HTTPStatus.BAD_REQUEST)
            return self.json_response({
                "range": range_name,
                "bucket_seconds": 7200,
                "points": self.app.db.history_heatmap(
                    server_id, int(time.time()) - seconds, 7200, timezone_offset,
                ),
            })
        return self._static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/servers/batch":
                return self._batch_servers(self.json_body())
            match = re.fullmatch(r"/api/servers/([^/]+)/collect", parsed.path)
            if match:
                self.app.scheduler.collect_now(match.group(1))
                return self.json_response({"accepted": True}, HTTPStatus.ACCEPTED)
            if parsed.path == "/api/collect":
                self.app.scheduler.collect_now()
                return self.json_response({"accepted": True}, HTTPStatus.ACCEPTED)
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            return self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            viewer = re.fullmatch(r"/api/viewers/([A-Za-z0-9_-]{8,80})", parsed.path)
            if viewer:
                body = self.json_body()
                state = self.app.scheduler.heartbeat(
                    viewer.group(1), int(body.get("interval", 10)), bool(body.get("visible", True)),
                )
                return self.json_response(state)
            server = re.fullmatch(r"/api/servers/([^/]+)", parsed.path)
            if server:
                body = self.json_body()
                if not self.app.db.set_server_enabled(server.group(1), bool(body.get("enabled"))):
                    return self.json_response({"error": "server not found"}, HTTPStatus.NOT_FOUND)
                return self.json_response({"ok": True})
        except ValueError as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        viewer = re.fullmatch(r"/api/viewers/([A-Za-z0-9_-]{8,80})", parsed.path)
        if viewer:
            self.app.scheduler.remove_lease(viewer.group(1))
            return self.json_response({"ok": True})
        server = re.fullmatch(r"/api/servers/([^/]+)", parsed.path)
        if server:
            deleted = self.app.db.delete_server(server.group(1))
            return self.json_response({"ok": deleted}, 200 if deleted else HTTPStatus.NOT_FOUND)
        self.json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _batch_servers(self, body: dict[str, Any]) -> None:
        entries = body.get("servers")
        passwords = body.get("passwords") or []
        if not isinstance(entries, list) or not entries or len(entries) > 200:
            raise ValueError("servers 必须包含 1 到 200 个条目")
        if not isinstance(passwords, list) or len(passwords) > 20 or not all(isinstance(item, str) for item in passwords):
            raise ValueError("passwords 最多包含 20 个字符串候选")
        results = []
        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    raise ValueError("服务器条目必须是对象")
                host = str(entry.get("host", "")).strip()
                port = int(entry.get("port", 22))
                username = str(entry.get("username", "root")).strip()
                self.app.adapter.validate_endpoint(host, port, username)
                server = self.app.db.upsert_server({
                    "id": uuid.uuid4().hex, "name": str(entry.get("name") or host).strip()[:120],
                    "host": host, "port": port, "username": username,
                    "tags": [str(tag)[:60] for tag in entry.get("tags", [])][:20],
                })
                auth = self.app.adapter.bootstrap_with_passwords(server, passwords)
                if auth["ok"]:
                    self.app.scheduler.collect_now(server["id"])
                else:
                    self.app.db.record_failure(server["id"], str(auth.get("error")), 0)
                results.append({"server": server, "auth": auth})
            except Exception as exc:  # noqa: BLE001
                results.append({"server": entry, "auth": {"ok": False, "error": str(exc)}})
        self.json_response({"results": results}, HTTPStatus.MULTI_STATUS)

    def _static(self, request_path: str) -> None:
        root = self.app.web_root
        if not root.is_dir():
            return self.json_response({"error": "前端尚未构建，请先运行 npm run build"}, HTTPStatus.SERVICE_UNAVAILABLE)
        relative = request_path.lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        if root not in candidate.parents and candidate != root:
            return self.json_response({"error": "invalid path"}, HTTPStatus.BAD_REQUEST)
        if not candidate.is_file():
            candidate = root / "index.html"
        if not candidate.is_file():
            return self.json_response({"error": "index.html not found"}, HTTPStatus.NOT_FOUND)
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: App) -> None:
        super().__init__(address, Handler)
        self.app = app
