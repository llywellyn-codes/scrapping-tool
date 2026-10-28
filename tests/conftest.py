import pytest

from prospect_finder.repository import Repository, connect, init_db


@pytest.fixture
def repo():
    conn = connect(":memory:")
    init_db(conn)
    yield Repository(conn, google_cache_days=0)
    conn.close()
