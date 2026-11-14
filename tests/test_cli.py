import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CommandTests(unittest.TestCase):
    def test_preview_and_raw_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable,'-m','schedule_bundles','demo'],capture_output=True,text=True,timeout=15)
            self.assertEqual(process.returncode,0,process.stderr)
            result = json.loads(process.stdout)
            self.assertIn('资料预览',result)
            self.assertGreater(result['原始资料']['bytes'],0)
            self.assertEqual(len(result['原始资料']['sha256']),64)

    def test_receive_query_and_compare(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / 'library'

            def run(*arguments):
                return subprocess.run(
                    [sys.executable, '-m', 'schedule_bundles', *arguments, '--store', str(store)],
                    capture_output=True, text=True, timeout=15, cwd=root)

            received = run('receive', str(root / 'fixtures' / 'publish-1.zip'))
            self.assertEqual(received.returncode, 0, received.stderr)
            first = json.loads(received.stdout)
            self.assertTrue(first['current'])

            day = run('service-day', '20261113')
            self.assertEqual(day.returncode, 0, day.stderr)
            body = json.loads(day.stdout)
            self.assertEqual([trip['trip_id'] for trip in body['trips']], ['t1'])
            self.assertTrue(body['trips'][0]['stop_times'][0]['arrival_time_next_day'])

            rejected = run('receive', str(root / 'fixtures' / 'publish-2.zip'))
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(json.loads(rejected.stdout)['errors'][0]['file'], 'stop_times.txt')

            current = run('current')
            self.assertEqual(json.loads(current.stdout)['publication_id'],
                             first['publication_id'])

            checked = run('check', str(root / 'fixtures' / 'publish-2.zip'))
            self.assertNotEqual(checked.returncode, 0)
            self.assertFalse(json.loads(checked.stdout)['accepted'])

            compared = run('compare', str(root / 'fixtures' / 'publish-1.zip'),
                           str(root / 'fixtures' / 'publish-2.zip'))
            self.assertEqual(compared.returncode, 0, compared.stderr)
            changes = json.loads(compared.stdout)['reference_changes']
            self.assertEqual(changes['removed'][0]['target_id'], 'platform')
            self.assertEqual(changes['added'][0]['target_id'], 'missing-platform')
            self.assertEqual(changes['added'][0]['source'], 'stop_times.txt:2')
