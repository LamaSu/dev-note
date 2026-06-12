import sys
from pathlib import Path

import pytest

# Make the package importable when tests run from the repo root without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devnote.config import Config
from devnote.registry import Registry


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.state_dir = str(tmp_path)
    c.suspect_threshold_s = 5.0
    c.hung_threshold_s = 10.0
    c.lease_ttl_s = 8.0
    c.dry_run = True            # tests must opt INTO arming explicitly
    c.allowed_uids = [1000]
    c.max_kills_per_window = 3
    return c


@pytest.fixture
def registry(cfg):
    return Registry(cfg.registry_path)
