from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from npu_fleet_monitor.probe import (
    attach_npu_telemetry,
    cpu_percent,
    is_device_busy,
    parse_disks,
    parse_docker,
    parse_meminfo,
    split_sections,
)
from npu_fleet_monitor.workspace_adapter import WorkspaceDeviceAdapter


class ProbeTests(unittest.TestCase):
    def test_split_and_host_parsers(self) -> None:
        sections = split_sections("noise\n__NFM_SECTION__meminfo\nMemTotal: 1000 kB\nMemAvailable: 250 kB")
        memory = parse_meminfo(sections["meminfo"])
        self.assertEqual(memory["memory_total_bytes"], 1024000)
        self.assertEqual(memory["memory_used_bytes"], 768000)
        self.assertEqual(cpu_percent((100, 40), (200, 60)), 80.0)

    def test_disk_parser(self) -> None:
        rows = parse_disks("Filesystem 1-blocks Used Available Capacity Mounted on\n/dev/sda 1000 800 200 80% /")
        self.assertEqual(rows[0]["used_percent"], 80)
        self.assertEqual(rows[0]["mount"], "/")

    def test_docker_stats_and_npu_telemetry(self) -> None:
        docker = parse_docker(
            '{"ID":"abc","Names":"worker","Image":"vllm","Status":"Up","State":"running"}',
            '{"Name":"worker","CPUPerc":"12.4%","MemUsage":"2GiB / 8GiB","PIDs":"10"}',
            '{"ServerVersion":"28.0","Driver":"overlay2","DockerRootDir":"/var/lib/docker"}',
        )
        self.assertEqual(docker["containers"][0]["stats"]["cpu_percent"], "12.4%")
        devices = [{"npu_id": 0}]
        attach_npu_telemetry(devices, "| 0 910B4 | OK 91.8 41 0 / 0 |")
        self.assertEqual(devices[0]["temperature_c"], 41)
        self.assertEqual(devices[0]["power_w"], 91.8)

    def test_busy_threshold_ignores_a3_driver_baseline(self) -> None:
        idle = {"processes": [], "aicore_percent": 0, "hbm": {"used_mb": 5989}}
        self.assertFalse(is_device_busy(idle, 8192))
        self.assertTrue(is_device_busy({**idle, "aicore_percent": 2}, 8192))
        self.assertTrue(is_device_busy({**idle, "hbm": {"used_mb": 8192}}, 8192))
        self.assertTrue(is_device_busy({**idle, "processes": [{"pid": 1}]}, 8192))

    def test_workspace_npu_parser_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            project = Path(__file__).resolve().parents[2]
            adapter = WorkspaceDeviceAdapter(project, Path(state))
            parsed = adapter.parse_npu(
                """
| NPU Name | Health Power(W) Temp(C) Hugepages-Usage |
| 0 910B4 | OK 91.8 41 0 / 0 |
| NPU Chip | Bus-Id AICore(%) Memory-Usage(MB) HBM-Usage(MB) |
| 0 0 | 0000:C1:00.0 87 1024 / 2048 32768 / 65536 |
""",
                "NPU ID : 0\nAicore Usage Rate(%) : 55\nHBM Usage Rate(%) : 50",
            )
            self.assertEqual(parsed["devices"][0]["aicore_percent"], 55)
            self.assertEqual(parsed["devices"][0]["hbm"]["used_mb"], 32768)

    def test_control_path_stays_below_unix_socket_limit(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            project = Path(__file__).resolve().parents[2]
            adapter = WorkspaceDeviceAdapter(project, Path(state))
            command = adapter.ssh_base({"host":"10.0.0.1","port":22,"username":"root"})
            option = next(command[index + 1] for index, value in enumerate(command) if value == "-o" and command[index + 1].startswith("ControlPath="))
            expanded = option.split("=", 1)[1].replace("%C", "x" * 40)
            self.assertLess(len(expanded), 100)

    def test_explicit_source_workspace_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
            workspace = Path(root)
            (workspace / ".agents/skills/machine-management").mkdir(parents=True)
            project = workspace / "detached-monitor-worktree"
            project.mkdir()
            with mock.patch.dict("os.environ", {"NFM_SOURCE_WORKSPACE": str(workspace)}):
                adapter = WorkspaceDeviceAdapter(project, Path(state))
            self.assertEqual(adapter.workspace_root, workspace)

    def test_workspace_discovery_includes_disabled_hosts_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
            workspace = Path(root)
            (workspace / ".agents/skills/machine-management").mkdir(parents=True)
            inventory_dir = workspace / ".vaws-local"
            inventory_dir.mkdir()
            (inventory_dir / "machine-inventory.json").write_text(json.dumps({
                "machines": [{
                    "alias": "active-a3",
                    "host": {"ip": "10.0.0.1", "port": 22, "user": "root", "machine_type": "A3"},
                }],
            }), encoding="utf-8")
            (workspace / "hosts.txt").write_text(
                "10.0.0.1 active-password\n10.0.0.2 disabled-password\n",
                encoding="utf-8",
            )
            project = workspace / "monitor"
            project.mkdir()
            with mock.patch.dict("os.environ", {"NFM_SOURCE_WORKSPACE": str(workspace)}):
                adapter = WorkspaceDeviceAdapter(project, Path(state))
                servers = adapter.discover_workspace_servers()

            self.assertEqual(len(servers), 2)
            active = next(server for server in servers if server["host"] == "10.0.0.1")
            disabled = next(server for server in servers if server["host"] == "10.0.0.2")
            self.assertTrue(active["workspace_enabled"])
            self.assertEqual(active["tags"], ["A3"])
            self.assertFalse(disabled["workspace_enabled"])
            self.assertEqual(disabled["tags"], ["低优先级"])
            self.assertNotIn("password", json.dumps(servers))


if __name__ == "__main__":
    unittest.main()
