from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.gldrubf_bot.paper import execute_paper
from tests.helpers import SQLiteResourceTestCase


class PaperTests(SQLiteResourceTestCase):
    def test_persistent_idempotence_and_flip(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "paper.db"
            first = execute_paper(db, "2026-11-01", 1, 100)
            self.assertTrue(first.executed)
            self.assertEqual(first.position_after, 1)
            retry = execute_paper(db, "2026-11-01", 1, 100)
            self.assertFalse(retry.executed)
            self.assertEqual(retry.position_after, 1)
            hold = execute_paper(db, "2026-11-02", 0, 110)
            self.assertEqual(hold.position_after, 1)
            flip = execute_paper(db, "2026-11-03", -1, 120)
            self.assertEqual((flip.position_before, flip.position_after), (1, -1))
            with self.assertRaises(ValueError):
                execute_paper(db, "2026-11-02", 1, 120)

    def test_reject_conflicting_replay(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "paper.db"
            execute_paper(db, "2026-11-01", 1, 100)
            with self.assertRaises(ValueError):
                execute_paper(db, "2026-11-01", -1, 100)


if __name__ == "__main__":
    unittest.main()
