import json
import tempfile
import unittest
from pathlib import Path
from visitor_counter import VisitorCounter


class VisitorCountTests(unittest.TestCase):
    def test_counter_persists_total_and_daily_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            count_file = Path(directory) / "visitor-count.json"
            counter = VisitorCounter(count_file)
            first = counter.increment()
            second = counter.increment()
            current = counter.read()

            self.assertEqual(first["total"], 1)
            self.assertEqual(second["total"], 2)
            self.assertEqual(current["total"], 2)
            self.assertEqual(current["today"], 2)
            self.assertEqual(json.loads(count_file.read_text(encoding="utf-8"))["total"], 2)

    def test_invalid_counter_file_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            count_file = Path(directory) / "visitor-count.json"
            count_file.write_text('{"total":"broken","days":{}}', encoding="utf-8")
            counter = VisitorCounter(count_file)
            with self.assertRaises(ValueError):
                counter.increment()


if __name__ == "__main__":
    unittest.main()
