from pathlib import Path
import tempfile
import unittest
import zipfile
from schedule_bundles.core import inspect_file


class PreviewTests(unittest.TestCase):
    def test_source_preview(self):
        result = inspect_file(Path('fixtures/publish-1.zip'))
        self.assertEqual(len(result['files']), 7)
        self.assertEqual(result['files'][0]['name'], 'agency.txt')

    def test_invalid_source_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'invalid'
            path.write_bytes(b'not a valid source')
            with self.assertRaises(zipfile.BadZipFile):
                inspect_file(path)

    def test_missing_source_is_not_empty_success(self):
        with self.assertRaises(FileNotFoundError):
            inspect_file(Path('fixtures/does-not-exist'))
