"""Polar 分层适配与存储兼容回归。输入为构造记录和临时旧 schema；输出为契约/迁移断言，不证明与官方 token 算法等价。"""

from __future__ import annotations

import json
import sqlite3

from sft_gateway.gateway.storage import Storage
from sft_gateway.protocols.responses_state import ResponsesStreamState
from sft_gateway.sse import SSEParser


def test_cr_only_sse_and_event_name_are_supported() -> None:
    parser = SSEParser()
    event = parser.feed(b'event: response.completed\rdata: {"ok":true}\r\r')[0]
    assert event.event == "response.completed"
    assert event.json() == {"ok": True}


def test_responses_state_keeps_items_when_terminal_response_omits_output() -> None:
    state = ResponsesStreamState()
    state.consume({"type": "response.created", "response": {"id": "r", "status": "in_progress", "output": []}})
    state.consume({"type": "response.output_item.added", "output_index": 0, "item": {"id": "m", "type": "message", "role": "assistant", "content": []}})
    state.consume({"type": "response.content_part.added", "item_id": "m", "content_index": 0, "part": {"type": "output_text", "text": ""}})
    state.consume({"type": "response.output_text.delta", "item_id": "m", "content_index": 0, "delta": "hello"})
    state.consume({"type": "response.completed", "response": {"id": "r", "status": "completed"}})
    snapshot = state.finalize()
    assert snapshot.status == "completed"
    assert snapshot.response and snapshot.response["output"][0]["content"][0]["text"] == "hello"


def test_storage_migrates_old_polar_schema(tmp_path) -> None:
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE sessions(session_id TEXT PRIMARY KEY, task_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, closed_at TEXT, metadata_json TEXT NOT NULL);
        CREATE TABLE requests(request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence INTEGER NOT NULL, protocol TEXT NOT NULL, path TEXT NOT NULL, model TEXT, request_json TEXT NOT NULL, request_headers_json TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL);
        CREATE TABLE responses(request_id TEXT PRIMARY KEY, status_code INTEGER, response_headers_json TEXT NOT NULL, response_json TEXT, raw_body BLOB, usage_json TEXT, finish_reason TEXT, error TEXT);
        CREATE TABLE stream_events(request_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_name TEXT, data_json TEXT, raw_frame BLOB NOT NULL, PRIMARY KEY(request_id, sequence));
        INSERT INTO sessions VALUES ('task', NULL, 'closed', 'now', NULL, '{}');
        INSERT INTO requests VALUES ('req', 'task', 1, 'responses', '/v1/responses', 'm', '{"input":"x"}', '{}', 'now', 'now', 'completed');
        INSERT INTO responses VALUES ('req', 200, '{}', '{"id":"resp"}', x'7B7D', NULL, NULL, NULL);
        """
    )
    db.commit(); db.close()
    storage = Storage(path)
    try:
        records = storage.records("task")
        assert records[0]["run_id"] == "task"
        assert records[0]["response_id"] == "resp"
        assert storage.db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == "3"
    finally:
        storage.close()
