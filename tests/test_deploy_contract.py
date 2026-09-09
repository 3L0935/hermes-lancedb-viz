import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployContractTests(unittest.TestCase):
    def test_local_deploy_syncs_canonical_runtime_and_systemd_viz(self):
        script = (ROOT / "scripts" / "deploy-local.sh").read_text()
        unit = (ROOT / "systemd" / "lancedb-viz.service").read_text()

        self.assertIn('CANONICAL="$HERMES_HOME/plugins/lancedb"', script)
        self.assertIn('RUNTIME="$HERMES_AGENT_HOME/plugins/memory/lancedb"', script)
        self.assertIn('VIZ="$HERMES_HOME/lancedb-viz"', script)
        self.assertIn("lancedb-viz.service", script)
        self.assertIn("--port 7778", unit)
        self.assertNotIn("docker restart", script)
        self.assertIn("--dry-run", script)


if __name__ == "__main__":
    unittest.main()
