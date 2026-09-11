import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import lancedb


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reembed-entries.py"


class ReembedEntriesTests(unittest.TestCase):
    def test_dry_run_reads_metadata_without_contacting_ollama(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = lancedb.connect(tmp)
            db.create_table("memories", data=[{
                "id": "12345678-abcd",
                "content": "Project:Alpha state=active [Tier=2]",
                "vector": [0.0] * 768,
            }])
            environment = dict(os.environ)
            environment["OLLAMA_HOST"] = "http://127.0.0.1:9"

            result = subprocess.run(
                [
                    str(Path(os.sys.executable)),
                    str(SCRIPT),
                    "--dry-run",
                    "--db-path",
                    tmp,
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        self.assertIn("Would embed 1 entries", result.stdout)
        self.assertNotIn("Ollama not reachable", result.stdout)


if __name__ == "__main__":
    unittest.main()
