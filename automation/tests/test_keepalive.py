from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
class KeepaliveTests(unittest.TestCase):
    def test_socket_options_and_connect_result_without_network(self):
        here = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as root:
            binary = str(Path(root, 'keepalive-test'))
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-o', binary,
                            str(here / 'keepalive_harness.c'), '-ldl'], check=True, timeout=15)
            result = subprocess.run([binary], capture_output=True, text=True, check=True, timeout=5)
            self.assertIn('no network', result.stdout)


if __name__ == '__main__':
    unittest.main()
