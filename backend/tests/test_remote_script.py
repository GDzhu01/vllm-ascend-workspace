from __future__ import annotations

import unittest

from npu_fleet_monitor.probe import FAST_SCRIPT, INFRA_SCRIPT


class RemoteScriptTests(unittest.TestCase):
    def test_core_probe_captures_its_own_status(self) -> None:
        self.assertIn("npu_info_rc", FAST_SCRIPT)
        self.assertNotIn("exit 0", FAST_SCRIPT)

    def test_optional_infrastructure_has_section_boundaries(self) -> None:
        for section in ("disk", "mounts", "docker", "docker_stats", "docker_info"):
            self.assertIn(f"__NFM_SECTION__{section}", INFRA_SCRIPT)


if __name__ == "__main__":
    unittest.main()
