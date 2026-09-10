import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployContractTests(unittest.TestCase):
    def test_local_deploy_targets_primary_docker_viz(self):
        script = (ROOT / "scripts" / "deploy-local.sh").read_text()
        unit = (ROOT / "systemd" / "lancedb-viz.service").read_text()

        self.assertIn('CANONICAL="$HERMES_HOME/plugins/lancedb"', script)
        self.assertIn('RUNTIME="$HERMES_AGENT_HOME/plugins/memory/lancedb"', script)
        self.assertIn('VIZ="$HERMES_HOME/lancedb-viz"', script)
        self.assertIn("lancedb-viz.service", script)
        self.assertIn("--port 7778", unit)
        self.assertIn("docker restart", script)
        self.assertIn("127.0.0.1:7777", script)
        self.assertIn("wait_for_http", script)
        self.assertIn("--dry-run", script)

    def test_verify_defaults_to_docker_7777_with_optional_systemd_mode(self):
        script = (ROOT / "scripts" / "verify-setup.sh").read_text()

        self.assertIn("http://127.0.0.1:7777", script)
        self.assertIn("LANCEDB_VIZ_MODE", script)
        self.assertIn('if [[ "$VIZ_MODE" == "systemd" ]]', script)


if __name__ == "__main__":
    unittest.main()
