import sqlite3

import pytest

from consume_selection.db import init_schema


@pytest.fixture
def db():
    """An empty in-memory l-space.db with the full schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c
