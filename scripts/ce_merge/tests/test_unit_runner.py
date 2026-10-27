# Copyright (c) 2026, Oracle and/or its affiliates.
"""Exercise the real CTest runner's timeout and persistent-failure reporting."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(shutil.which('ctest'), 'CTest required for runner regression')
class UnitRunnerTests(unittest.TestCase):
    def test_hung_test_times_out_and_remains_a_failure_after_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build/mysql-test/var').mkdir(parents=True)
            (root / 'build/CTestTestfile.cmake').write_text(
                f'add_test(hung_test "{sys.executable}" "-c" "import time; time.sleep(3)")\n')
            wrapper = root / 'ctest'
            # Shorten the runner's 120-second budget without changing its flags.
            # --test-timeout would let this test pass instead of timing out.
            wrapper.write_text(f'#!{sys.executable}\nimport os, sys\n'
                               f'os.execv({shutil.which("ctest")!r}, ["ctest"] + '
                               '["1" if arg == "120" else arg for arg in sys.argv[1:]])\n')
            wrapper.chmod(0o755)
            summary = root / 'summary.md'
            result = subprocess.run(['bash', str(ROOT / 'scripts/ci/run_unit_tests.sh')],
                                    cwd=root, env=dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                                                       GITHUB_STEP_SUMMARY=str(summary)),
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Timeout', result.stdout)
            self.assertEqual((root / 'build/mysql-test/var/ctest-persistent-failures.log').read_text(),
                             'hung_test\n')
            self.assertIn('Persistent failures:\n- hung_test', summary.read_text())
