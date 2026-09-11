"""Shared pytest fixtures and environment setup.

The package uses a src-layout (``src/google_search_mcp``). ``pyproject.toml``
sets ``pythonpath = ["src"]`` so ``import google_search_mcp`` works without
manual ``sys.path`` manipulation.

Some modules read env vars at import time (e.g. ``FEEDS_DB_PATH``). We set
safe defaults here so tests don't touch the user's real data.
"""

import os

# Use a throwaway feeds DB for tests so we never touch the real one.
os.environ.setdefault("FEEDS_DB_PATH", "/tmp/test_pytest_feeds.db")
# Skip cookie validation at import time (no real Google cookies in CI).
os.environ.setdefault("SKIP_COOKIE_VALIDATION", "1")
# Keep the health server off during unit tests unless a test opts in.
os.environ.setdefault("ENABLE_HEALTH_SERVER", "0")
