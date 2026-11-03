import os
import sys
import pytest
from unittest.mock import MagicMock, AsyncMock

# Force TESTING environment mode BEFORE importing app files to skip startup config checks
os.environ["TESTING"] = "true"
os.environ["MONGODB_URI"] = "mongodb://mock"
os.environ["AZURE_SQL_HOST"] = "mock_sql"
os.environ["AZURE_SQL_DB"] = "mock_db"
os.environ["AZURE_SQL_USER"] = "mock_user"
os.environ["AZURE_SQL_PASSWORD"] = "mock_pass"
os.environ["AZURE_CLIENT_ID"] = "mock_client"
os.environ["AZURE_TENANT_ID"] = "mock_tenant"
# Force auth enforcement in tests (overrides any DEV_AUTH_BYPASS=true from a local .env)
os.environ["DEV_AUTH_BYPASS"] = "false"
# Disable the billing scheduler/backfill during tests (no billing creds needed)
os.environ["BILLING_SYNC_ENABLED"] = "false"
# Disable the AI Analytics Worker during tests — Phase 1 no-op stub is tested
# directly via run_worker/stop_worker_task, not through the lifespan.
os.environ["AI_ANALYTICS_WORKER_ENABLED"] = "false"
# Force the direct-read path in tests. The local .env may set
# AI_ANALYTICS_USE_PROJECTION=true (operator toggle for production), which
# would leak into the test suite via load_dotenv() in config.py and route
# _load_normalized_cohort through the projection path (empty mongomock)
# instead of the mocked direct-read path. Phase 10 tests that exercise the
# projection path override this per-test via monkeypatch.
os.environ["AI_ANALYTICS_USE_PROJECTION"] = "false"

# Ensure backend directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mongomock


# ---------------------------------------------------------------------------
# Async wrappers around mongomock so that the application code (which uses Motor
# async semantics like ``await cursor.to_list()``, ``await collection.find_one()``)
# can work transparently with a synchronous in-memory mongomock backend.
# ---------------------------------------------------------------------------

class _AsyncCursor:
    """Wraps a mongomock Cursor to support ``await cursor.to_list(length=...)``."""

    def __init__(self, cursor):
        self._cursor = cursor

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._cursor)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, length=None):
        items = list(self._cursor)
        if length is not None:
            return items[:length]
        return items


class _AsyncCollection:
    """Wraps a mongomock Collection so that query/mutation methods are awaitable."""

    def __init__(self, collection):
        self._col = collection

    def find(self, *args, **kwargs):
        cursor = self._col.find(*args, **kwargs)
        return _AsyncSortableCursor(cursor)

    async def find_one(self, *args, **kwargs):
        return self._col.find_one(*args, **kwargs)

    async def replace_one(self, *args, **kwargs):
        return self._col.replace_one(*args, **kwargs)

    async def insert_one(self, *args, **kwargs):
        return self._col.insert_one(*args, **kwargs)

    async def insert_many(self, *args, **kwargs):
        return self._col.insert_many(*args, **kwargs)

    async def find_one_and_update(self, *args, **kwargs):
        from pymongo import ReturnDocument
        # mongomock uses return_document kwarg
        return self._col.find_one_and_update(*args, **kwargs)

    async def update_one(self, *args, **kwargs):
        return self._col.update_one(*args, **kwargs)

    async def update_many(self, *args, **kwargs):
        return self._col.update_many(*args, **kwargs)

    async def delete_one(self, *args, **kwargs):
        return self._col.delete_one(*args, **kwargs)

    async def delete_many(self, *args, **kwargs):
        return self._col.delete_many(*args, **kwargs)

    async def count_documents(self, *args, **kwargs):
        return self._col.count_documents(*args, **kwargs)

    async def distinct(self, *args, **kwargs):
        return self._col.distinct(*args, **kwargs)

    def aggregate(self, *args, **kwargs):
        return _AsyncCursor(self._col.aggregate(*args, **kwargs))

    async def create_index(self, *args, **kwargs):
        return self._col.create_index(*args, **kwargs)


class _AsyncSortableCursor(_AsyncCursor):
    """Supports chaining ``.sort()`` and ``.limit()`` before ``.to_list()``."""

    def __init__(self, cursor):
        super().__init__(cursor)

    def sort(self, *args, **kwargs):
        self._cursor = self._cursor.sort(*args, **kwargs)
        return self

    def limit(self, n, *args, **kwargs):
        self._cursor = self._cursor.limit(n, *args, **kwargs)
        return self


class _AsyncDatabase:
    """Wraps a mongomock Database so that ``db["collection"]`` returns an _AsyncCollection."""

    def __init__(self, db):
        self._db = db

    def __getitem__(self, name):
        return _AsyncCollection(self._db[name])

    def __getattr__(self, name):
        return _AsyncCollection(self._db[name])


@pytest.fixture
def mock_mongo_db():
    """Provides a mongomock client wrapped with async helpers to simulate Motor."""
    client = mongomock.MongoClient()
    db = client["test_db"]
    return _AsyncDatabase(db)


@pytest.fixture(autouse=True)
def mock_db_manager(monkeypatch, mock_mongo_db):
    """Intercepts db_manager references to route query operations to our mock mongo."""
    from database import db_manager

    # Override connect/disconnect
    db_manager.connect = MagicMock()
    db_manager.disconnect = MagicMock()
    db_manager.init_indexes = AsyncMock()
    db_manager.db = mock_mongo_db
    db_manager.ai_db = mock_mongo_db

    # Force get_db dependency to yield the mock db
    monkeypatch.setattr("database.db_manager.db", mock_mongo_db)


@pytest.fixture(autouse=True)
def mock_entra_verification(monkeypatch):
    """Bypasses active Microsoft authentication redirects, returning standard test claims."""
    from auth import TokenVerifier

    # Mock JWKS fetches and decodes
    monkeypatch.setattr(TokenVerifier, "_fetch_jwks", lambda self: {"keys": [{"kid": "test-kid"}]})
    monkeypatch.setattr(TokenVerifier, "get_public_key", lambda self, kid: {"kid": kid})

    # Custom decode returns standard claims
    def mock_verify(self, token: str) -> dict:
        if token == "invalid-token":
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Token signature verification failed.")
        return {
            "preferred_username": "john.doe@streamlineas.com",
            "upn": "john.doe@streamlineas.com",
            "name": "John Doe",
            "iss": "https://login.microsoftonline.com/mock_tenant/v2.0",
            "aud": "mock_client"
        }

    monkeypatch.setattr(TokenVerifier, "verify_token", mock_verify)


@pytest.fixture(autouse=True)
def reset_target_db_cache():
    """Ensures each test starts with a clean SQL column-resolution cache."""
    from target_db import target_db

    target_db._claims_date_column = None
    target_db._claims_column_map = None


@pytest.fixture
def test_client():
    """Provides a synchronous HTTP client for integration test endpoint hits."""
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)


def make_projection_doc(claim_id, sql_row=None, ai_record=None, **extra):
    """Build an ``ai_invoice_analytics`` projection doc from old fixture shapes.

    The outcomes cohort is Mongo-first: tests that used to patch the SQL
    cohort + direct Mongo read now seed projection docs instead. This
    helper merges the two old fixture shapes (SQL row + ai_record) into
    the projection field names (Phase 0 plan Section 9) so existing test
    data translates mechanically.
    """
    sql_row = sql_row or {}
    ai_record = ai_record or {}
    doc = {
        "_id": claim_id,
        "claim_id": claim_id,
        "department_id": sql_row.get("dept_id"),
        "department_name": sql_row.get("department_name"),
        "run_number": sql_row.get("run_number"),
        "ai_inserted_at": sql_row.get("ai_business_updated_at"),
        "ai_updated_at": sql_row.get("ai_business_updated_at"),
        "has_ai_line_item_record": True,
        "ai_processing_status": ai_record.get("claim_processing_status"),
        "agent_execution_status": ai_record.get("agent_exec_status"),
        "ai_invoice_total": ai_record.get("invoice_total"),
        "processing_duration_seconds": ai_record.get("processing_time_seconds"),
        "confidence_level": ai_record.get("confidence_level"),
        "is_billable": ai_record.get("is_billable"),
        "billing_category": ai_record.get("billing_category"),
        "billing_level": ai_record.get("billing_level"),
        "level_identification_confidence": ai_record.get("level_identification_confidence"),
        "level_identification_low_confidence": ai_record.get("level_identification_low_confidence"),
        "dept_send_auto_invoice_status": ai_record.get("dept_send_auto_invoice_status"),
        "is_cancelled": ai_record.get("is_cancelled"),
        "cancellation_reason": ai_record.get("cancellation_reason"),
        "line_items_save_to_rh_status": ai_record.get("line_items_save_to_rh_status"),
        "retry_count": ai_record.get("retry_count"),
    }
    doc.update(extra)
    return doc
