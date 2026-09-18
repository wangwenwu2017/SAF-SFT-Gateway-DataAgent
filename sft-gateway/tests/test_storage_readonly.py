"""存储只读连接回归。输入为临时 DB 和只读 Storage；输出为读写权限及 schema 断言，不覆盖 CLI 导入 app 触发的默认数据库写入。"""

from __future__ import annotations

import sqlite3

import pytest

from sft_gateway.gateway.storage import Storage


def test_read_only_storage_does_not_take_schema_lock(tmp_path):
    """A live export can read while the writer holds an uncommitted write."""
    path = tmp_path / "capture.sqlite"
    writer = Storage(path)
    try:
        request_id = "req-1"
        writer.start_request(request_id, "run-1", "responses", "/v1/responses", "m", {"input": "x"}, {})
        writer.finish(request_id, status="completed", status_code=200, response={"id": "r1", "output": []}, raw_body=b"{}", response_id="r1", previous_response_id=None)
        # Hold a transaction open to model an active gateway writer.  The
        # read-only connection must not attempt schema migration or wait for
        # this write lock.
        writer.db.execute("BEGIN IMMEDIATE")
        writer.db.execute("UPDATE requests SET status='in_flight' WHERE request_id=?", (request_id,))
        reader = Storage(path, read_only=True)
        try:
            rows = reader.records("run-1")
            assert len(rows) == 1
            assert rows[0]["request_id"] == request_id
        finally:
            reader.close()
    finally:
        writer.db.rollback()
        writer.close()


def test_read_only_storage_rejects_legacy_schema_without_migrating(tmp_path):
    path = tmp_path / "legacy.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE requests (request_id TEXT)")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="read-only capture export"):
        Storage(path, read_only=True)

