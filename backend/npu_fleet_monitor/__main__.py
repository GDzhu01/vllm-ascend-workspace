from __future__ import annotations

import signal
import threading
import hashlib

from .api import App, AppServer
from .db import Database
from .probe import HostProbe
from .scheduler import AdaptiveScheduler
from .settings import Settings
from .workspace_adapter import WorkspaceDeviceAdapter


def main() -> None:
    settings = Settings.load()
    settings.prepare()
    db = Database(settings.state_dir / "monitor.sqlite3")
    db.initialize()
    adapter = WorkspaceDeviceAdapter(settings.project_root, settings.state_dir)
    adapter.ensure_key()
    probe = HostProbe(adapter, settings.ssh_timeout, settings.hbm_busy_threshold_mb)
    scheduler = AdaptiveScheduler(settings, db, probe)
    app = App(settings, db, adapter, scheduler)
    server = AppServer((settings.bind, settings.port), app)

    def stop(*_: object) -> None:
        # BaseServer.shutdown() must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, name="nfm-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    scheduler.start()

    def import_workspace_inventory() -> None:
        existing = {
            (item["host"], int(item["port"]), item["username"]): item
            for item in db.list_servers()
        }
        for item in adapter.discover_workspace_servers():
            endpoint = (item["host"], int(item["port"]), item["username"])
            server_record = existing.get(endpoint)
            if server_record is None:
                token = "|".join(map(str, endpoint)).encode()
                server_record = db.upsert_server({**item, "id": hashlib.sha256(token).hexdigest()[:32]})
            auth = adapter.bootstrap_with_passwords(server_record, [])
            if auth.get("ok"):
                scheduler.collect_now(server_record["id"])
            else:
                db.record_failure(server_record["id"], str(auth.get("error") or "工作区密钥不可用"), 0)
            existing[endpoint] = server_record

    threading.Thread(target=import_workspace_inventory, name="nfm-inventory-import", daemon=True).start()
    print(f"NPU Fleet Monitor: http://{settings.bind}:{settings.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        server.server_close()


if __name__ == "__main__":
    main()
