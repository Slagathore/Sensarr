import os
import sys
import tempfile
from pathlib import Path

# IMPORTANT: this runs at conftest IMPORT time, before pytest imports any test
# module. config.py resolves APP_DB_PATH the moment it is first imported, so
# the env var must be set here at module level — a session fixture is too late
# and the tests would write into the real application database.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="prb-tests-")
os.environ["APP_DB_PATH"] = str(Path(_TEST_DB_DIR) / "test_app.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
