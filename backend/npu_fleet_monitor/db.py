from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  host TEXT NOT NULL,
  port INTEGER NOT NULL DEFAULT 22,
  username TEXT NOT NULL DEFAULT 'root',
  tags_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_seen_at INTEGER,
  last_error TEXT,
  UNIQUE(host, port, username)
);
CREATE TABLE IF NOT EXISTS host_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  collected_at INTEGER NOT NULL,
  duration_ms INTEGER,
  cpu_percent REAL,
  load1 REAL,
  load5 REAL,
  load15 REAL,
  memory_used_bytes INTEGER,
  memory_total_bytes INTEGER,
  swap_used_bytes INTEGER,
  swap_total_bytes INTEGER,
  npu_util_percent REAL,
  hbm_used_mb INTEGER,
  hbm_total_mb INTEGER,
  npu_count INTEGER NOT NULL DEFAULT 0,
  busy_npu_count INTEGER NOT NULL DEFAULT 0,
  docker_running INTEGER,
  disk_max_percent REAL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_samples_server_time
  ON host_samples(server_id, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_host_samples_time
  ON host_samples(collected_at);
CREATE TABLE IF NOT EXISTS collection_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  collected_at INTEGER NOT NULL,
  status TEXT NOT NULL,
  duration_ms INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_collection_events_server_time
  ON collection_events(server_id, collected_at DESC);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=15000")
            self._local.connection = connection
        return connection

    def initialize(self) -> None:
        connection = self.connection()
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA optimize")
        connection.commit()

    @staticmethod
    def _server(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["tags"] = json.loads(result.pop("tags_json") or "[]")
        return result

    def list_servers(self) -> list[dict[str, Any]]:
        rows = self.connection().execute("SELECT * FROM servers ORDER BY name COLLATE NOCASE").fetchall()
        return [self._server(row) for row in rows]

    def get_server(self, server_id: str) -> dict[str, Any] | None:
        row = self.connection().execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
        return self._server(row) if row else None

    def upsert_server(self, server: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        connection = self.connection()
        connection.execute(
            """
            INSERT INTO servers(id, name, host, port, username, tags_json, enabled, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(host, port, username) DO UPDATE SET
              name=excluded.name, tags_json=excluded.tags_json, enabled=1, updated_at=excluded.updated_at
            """,
            (
                server["id"], server["name"], server["host"], server["port"], server["username"],
                json.dumps(server.get("tags", []), ensure_ascii=False), now, now,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM servers WHERE host=? AND port=? AND username=?",
            (server["host"], server["port"], server["username"]),
        ).fetchone()
        return self._server(row)

    def set_server_enabled(self, server_id: str, enabled: bool) -> bool:
        return self.update_server(server_id, enabled=enabled)

    def update_server(
        self,
        server_id: str,
        *,
        enabled: bool | None = None,
        tags: list[str] | None = None,
    ) -> bool:
        assignments = ["updated_at=?"]
        values: list[Any] = [int(time.time())]
        if enabled is not None:
            assignments.append("enabled=?")
            values.append(int(enabled))
        if tags is not None:
            assignments.append("tags_json=?")
            values.append(json.dumps(tags, ensure_ascii=False))
        values.append(server_id)
        cursor = self.connection().execute(
            f"UPDATE servers SET {', '.join(assignments)} WHERE id=?",
            values,
        )
        self.connection().commit()
        return cursor.rowcount > 0

    def delete_server(self, server_id: str) -> bool:
        cursor = self.connection().execute("DELETE FROM servers WHERE id=?", (server_id,))
        self.connection().commit()
        return cursor.rowcount > 0

    def record_success(self, server_id: str, snapshot: dict[str, Any], persist_sample: bool) -> None:
        now = int(snapshot["collected_at"])
        connection = self.connection()
        connection.execute(
            "UPDATE servers SET last_seen_at=?, last_error=NULL, updated_at=? WHERE id=?",
            (now, now, server_id),
        )
        if persist_sample:
            connection.execute(
                "INSERT INTO collection_events(server_id,collected_at,status,duration_ms) VALUES(?,?,?,?)",
                (server_id, now, "ok", snapshot.get("duration_ms")),
            )
            summary = snapshot.get("summary", {})
            connection.execute(
                """
                INSERT INTO host_samples(
                  server_id,collected_at,duration_ms,cpu_percent,load1,load5,load15,
                  memory_used_bytes,memory_total_bytes,swap_used_bytes,swap_total_bytes,
                  npu_util_percent,hbm_used_mb,hbm_total_mb,npu_count,busy_npu_count,
                  docker_running,disk_max_percent,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    server_id, now, snapshot.get("duration_ms"), summary.get("cpu_percent"),
                    summary.get("load1"), summary.get("load5"), summary.get("load15"),
                    summary.get("memory_used_bytes"), summary.get("memory_total_bytes"),
                    summary.get("swap_used_bytes"), summary.get("swap_total_bytes"),
                    summary.get("npu_util_percent"), summary.get("hbm_used_mb"),
                    summary.get("hbm_total_mb"), summary.get("npu_count", 0),
                    summary.get("busy_npu_count", 0), summary.get("docker_running"),
                    summary.get("disk_max_percent"), json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        connection.commit()

    def record_failure(self, server_id: str, error: str, duration_ms: int, persist_event: bool = True) -> None:
        now = int(time.time())
        safe_error = error[-1200:]
        connection = self.connection()
        connection.execute(
            "UPDATE servers SET last_error=?, updated_at=? WHERE id=?",
            (safe_error, now, server_id),
        )
        if persist_event:
            connection.execute(
                "INSERT INTO collection_events(server_id,collected_at,status,duration_ms,error) VALUES(?,?,?,?,?)",
                (server_id, now, "failed", duration_ms, safe_error),
            )
        connection.commit()

    def history(self, server_id: str | None, since: int, bucket_seconds: int) -> list[dict[str, Any]]:
        where = "collected_at >= ?"
        params: list[Any] = [since]
        if server_id:
            where += " AND server_id = ?"
            params.append(server_id)
        rows = self.connection().execute(
            f"""
            SELECT (collected_at / ?) * ? AS bucket,
                   AVG(cpu_percent) AS cpu_percent,
                   AVG(npu_util_percent) AS npu_util_percent,
                   AVG(CASE WHEN memory_total_bytes > 0 THEN memory_used_bytes * 100.0 / memory_total_bytes END) AS memory_percent,
                   AVG(CASE WHEN hbm_total_mb > 0 THEN hbm_used_mb * 100.0 / hbm_total_mb END) AS hbm_percent,
                   MAX(disk_max_percent) AS disk_max_percent,
                   AVG(busy_npu_count) AS busy_npu_count,
                   MAX(npu_count) AS npu_count
            FROM host_samples WHERE {where}
            GROUP BY bucket ORDER BY bucket
            """,
            [bucket_seconds, bucket_seconds, *params],
        ).fetchall()
        return [dict(row) for row in rows]

    def history_heatmap(
        self,
        server_id: str,
        since: int,
        bucket_seconds: int = 7200,
        timezone_offset_seconds: int = 0,
    ) -> list[dict[str, Any]]:
        offset = max(-50400, min(50400, int(timezone_offset_seconds)))
        bucket = max(3600, int(bucket_seconds))
        bucket_expression = "((collected_at + ?) / ?) * ? - ?"
        summary_rows = self.connection().execute(
            f"""
            SELECT {bucket_expression} AS bucket,
                   COUNT(*) AS sample_count,
                   AVG(cpu_percent) AS cpu_percent,
                   AVG(npu_util_percent) AS npu_util_percent,
                   AVG(CASE WHEN memory_total_bytes > 0 THEN memory_used_bytes * 100.0 / memory_total_bytes END) AS memory_percent,
                   AVG(CASE WHEN hbm_total_mb > 0 THEN hbm_used_mb * 100.0 / hbm_total_mb END) AS hbm_percent,
                   MAX(disk_max_percent) AS disk_max_percent
            FROM host_samples
            WHERE server_id = ? AND collected_at >= ?
            GROUP BY bucket ORDER BY bucket
            """,
            (offset, bucket, bucket, offset, server_id, since),
        ).fetchall()
        points = {int(row["bucket"]): {**dict(row), "devices": []} for row in summary_rows}
        if not points:
            return []

        device_rows = self.connection().execute(
            f"""
            SELECT {bucket_expression} AS bucket,
                   CAST(json_extract(device.value, '$.npu_id') AS INTEGER) AS npu_id,
                   MAX(COALESCE(json_extract(device.value, '$.name'), 'Ascend NPU')) AS name,
                   AVG(CAST(json_extract(device.value, '$.aicore_percent') AS REAL)) AS utilization_percent,
                   AVG(CASE
                       WHEN CAST(json_extract(device.value, '$.hbm.total_mb') AS REAL) > 0
                       THEN CAST(json_extract(device.value, '$.hbm.used_mb') AS REAL) * 100.0 /
                            CAST(json_extract(device.value, '$.hbm.total_mb') AS REAL)
                   END) AS hbm_percent,
                   AVG(CASE WHEN json_extract(device.value, '$.busy') THEN 100.0 ELSE 0.0 END) AS busy_percent
            FROM host_samples
            JOIN json_each(host_samples.payload_json, '$.devices') AS device
            WHERE server_id = ? AND collected_at >= ?
            GROUP BY bucket, npu_id ORDER BY bucket, npu_id
            """,
            (offset, bucket, bucket, offset, server_id, since),
        ).fetchall()
        for row in device_rows:
            point = points.get(int(row["bucket"]))
            if point is not None:
                point["devices"].append({key: row[key] for key in row.keys() if key != "bucket"})
        return [points[key] for key in sorted(points)]

    def latest_persisted(self) -> dict[str, int]:
        rows = self.connection().execute(
            "SELECT server_id, MAX(collected_at) AS collected_at FROM host_samples GROUP BY server_id"
        ).fetchall()
        return {str(row["server_id"]): int(row["collected_at"]) for row in rows if row["collected_at"]}

    def prune(self, retention_days: int) -> None:
        cutoff = int(time.time()) - retention_days * 86400
        connection = self.connection()
        connection.execute("DELETE FROM host_samples WHERE collected_at < ?", (cutoff,))
        connection.execute("DELETE FROM collection_events WHERE collected_at < ?", (cutoff,))
        connection.commit()
        connection.execute("PRAGMA optimize")
