"""Run every check. SQLite by default; set DATABASE_URL=postgresql://... to run against Postgres.

  1. tests/verify.py          18 cases from ground truth (rules and ledger only)
  2. tests/regressions.py     review regressions (retry, names, messages, rejection audit, mixed tax)
  3. tests/test_extract.py    model guesses, hallucinations, failures, number formats, OCR conflicts
  4. tests/test_end_to_end.py 18 cases from the PDFs: page reading, OCR, value checks and engine
"""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))

from engine import Ledger  # noqa: E402
from regressions import check_review  # noqa: E402
from verify import verify  # noqa: E402
import test_end_to_end  # noqa: E402
import test_extract  # noqa: E402

if __name__ == "__main__":
    with TemporaryDirectory() as tmp:
        path = os.environ.get("DATABASE_URL") or Path(tmp) / "acceptance.db"
        print(f"Database: {'Postgres' if str(path).startswith('postgres') else 'SQLite'}\n")
        verify(path)
        check_review(path)
        print()
        test_extract.check_all(Ledger(path))
        print()
        test_end_to_end.check_all(Ledger(path))
        print("\nAll checks passed.")
