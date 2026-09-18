"""SQLite 捕获存储。输入请求、事件和响应，输出按记录顺序读取的一致快照。
每个写操作使用短事务；不跨网络等待持锁。HTTP 层在线程中调用，逐次等待形成背压。
read_only=True 不建库/迁移。schema v3 增加 request_wire，旧记录 bytes 标为不可用。"""

from __future__ import annotations

import json
import base64
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote



def utc_now() -> str:
    """返回带时区的 UTC ISO-8601 时间字符串，供请求开始/结束时间使用。"""
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """单库采集存储。输入数据库路径和可选镜像归档路径，输出 records 数据。
    SQLite 是权威记录，JSONL 是可选副本；写入模式可迁移旧表，读取模式拒绝迁移。"""
    def __init__(
        self,
        path: str | Path,
        *,
        raw_archive_path: str | Path | None = None,
        read_only: bool = False,
    ) -> None:
        """打开 path 指向的捕获数据库，并保留连接供后续读写。

        默认模式会建目录、启用 WAL 并迁移 schema，因而可能获取 schema 锁；实时
        导出应使用 read_only，以 SQLite ``mode=ro`` 打开并校验已有 schema。
        只读模式遇到文件缺失或旧 schema 会报错，不自行初始化或迁移数据库。
        raw_archive_path 指定可选追加归档文件，其父目录在初始化时创建。
        """
        self.path = Path(path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_archive_path = Path(raw_archive_path) if raw_archive_path else None
        if self.raw_archive_path and not read_only:
            self.raw_archive_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._pending_events = 0
        self.read_only = read_only
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(f"capture database does not exist: {self.path}")
            uri = f"file:{quote(self.path.resolve().as_posix(), safe='/')}?mode=ro"
            self.db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if read_only:
            self.db.execute("PRAGMA query_only=ON")
            try:
                self._validate_read_only_schema()
            except Exception:
                self.db.close()
                raise
        else:
            # A busy timeout lets independent gateway workers wait for the
            # short write transaction instead of failing immediately with
            # ``database is locked``.
            self.db.execute("PRAGMA busy_timeout=30000")
            self.db.execute("PRAGMA journal_mode=WAL")
            self._backup_before_migration()
            self._ensure_schema()

    def _backup_before_migration(self) -> None:
        """旧 schema 写入前创建 SQLite 一致备份；新库或 v3 返回，备份已存在则报错。"""
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return
        version = self.db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone() if 'schema_meta' in tables else None
        if version and version[0] == '3':
            return
        backup = self.path.with_name(self.path.name + '.pre-v3.bak')
        if backup.exists():
            self.db.close()
            raise RuntimeError(f"migration backup already exists: {backup}; inspect before retrying")
        with sqlite3.connect(backup) as destination:
            self.db.backup(destination)

    def _validate_read_only_schema(self) -> None:
        """验证当前 schema 的必需表/列；缺失时抛 RuntimeError，不尝试迁移。"""
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"requests", "responses", "stream_events"}
        if not required <= tables:
            missing = ", ".join(sorted(required - tables))
            raise RuntimeError(
                f"read-only capture export requires the current schema; missing tables: {missing}. "
                "Run one normal gateway open once to migrate the database."
            )
        for table, columns in (("requests", self._REQUEST_COLUMNS), ("responses", self._RESPONSE_COLUMNS), ("stream_events", self._EVENT_COLUMNS)):
            actual = self._columns(table)
            if not columns <= actual:
                missing = ", ".join(sorted(columns - actual))
                raise RuntimeError(f"read-only capture export cannot migrate legacy table {table}; missing columns: {missing}")

    _REQUEST_COLUMNS = {
        "request_id", "run_id", "sequence", "protocol", "path", "model",
        "request_json", "headers_json", "started_at", "status", "finished_at",
    }
    _RESPONSE_COLUMNS = {
        "request_id", "status_code", "response_json", "raw_body", "response_id",
        "previous_response_id", "terminal_status", "error",
    }
    _EVENT_COLUMNS = {"request_id", "sequence", "event_name", "data_json", "raw_frame"}

    def _ensure_schema(self) -> None:
        """创建 v3 表，并保留与首版 Polar-shaped 数据库的迁移入口。

        requests 保存请求及 run 内序号，responses 按 request_id 保存单个终态
        响应，stream_events 按 (request_id, sequence) 保存原始流帧。旧版使用
        session_id/request_headers_json 等字段，不能仅依赖 IF NOT EXISTS；
        本方法给不兼容表加时间戳改名，再复制可明确映射的字段并提交 schema v3。
        """
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            requests_ok = "requests" in tables and self._columns("requests") >= self._REQUEST_COLUMNS
            responses_ok = "responses" in tables and self._columns("responses") >= self._RESPONSE_COLUMNS
            events_ok = "stream_events" in tables and self._columns("stream_events") >= self._EVENT_COLUMNS
            legacy: dict[str, str] = {}
            if not requests_ok and "requests" in tables:
                legacy["requests"] = self._archive_table("requests")
            if not responses_ok and "responses" in tables:
                legacy["responses"] = self._archive_table("responses")
            if not events_ok and "stream_events" in tables:
                legacy["stream_events"] = self._archive_table("stream_events")
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
              request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
              protocol TEXT NOT NULL, path TEXT NOT NULL, model TEXT, request_json TEXT NOT NULL,
              headers_json TEXT NOT NULL, started_at TEXT NOT NULL, status TEXT NOT NULL,
              finished_at TEXT, UNIQUE(run_id, sequence));
            CREATE TABLE IF NOT EXISTS responses (
              request_id TEXT PRIMARY KEY, status_code INTEGER, response_json TEXT,
              raw_body BLOB, response_id TEXT, previous_response_id TEXT,
              terminal_status TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS stream_events (
              request_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_name TEXT,
              data_json TEXT, raw_frame BLOB NOT NULL, PRIMARY KEY(request_id, sequence));
            """)
            self.db.execute("CREATE TABLE IF NOT EXISTS request_wire (request_id TEXT PRIMARY KEY, request_body BLOB, forwarded_body BLOB)")
            if legacy:
                self._migrate_legacy(legacy)
            self.db.execute("INSERT OR REPLACE INTO schema_meta VALUES ('version', '3')")
            self.db.commit()

    def _columns(self, table: str) -> set[str]:
        """输入内部可信表名，查询 PRAGMA 并返回列名集合；不修改 schema。

        table 直接用于 SQL 标识符，不能传入未经验证的外部请求参数。
        """
        return {str(row[1]) for row in self.db.execute(f"PRAGMA table_info({table})")}

    def _archive_table(self, table: str) -> str:
        """输入内部旧表名，执行 ALTER TABLE 归档并返回归档名。

        修改当前连接的 schema，事务提交由外层管理；SQL 失败会向调用方传播。
        """
        suffix = str(int(time.time() * 1000))
        name = f"legacy_{table}_{suffix}"
        self.db.execute(f"ALTER TABLE {table} RENAME TO {name}")
        return name

    def _migrate_legacy(self, legacy: dict[str, str]) -> None:
        """输入旧表类型到归档表名的映射，将已识别字段复制到 v2 表；返回 None。

        执行 INSERT，不删除归档表；未知结构跳过，SQL 错误传播，由外层管理事务。
        不补造缺失的请求正文、时间或上下文。
        """
        requests_table = legacy.get("requests")
        responses_table = legacy.get("responses")
        events_table = legacy.get("stream_events")
        if requests_table:
            columns = self._columns(requests_table)
            if {"request_id", "session_id", "sequence", "protocol", "path", "request_json", "started_at", "status"} <= columns:
                header_col = "request_headers_json" if "request_headers_json" in columns else "headers_json"
                self.db.execute(
                    f"INSERT OR IGNORE INTO requests "
                    f"(request_id,run_id,sequence,protocol,path,model,request_json,headers_json,started_at,status,finished_at) "
                    f"SELECT request_id,session_id,sequence,protocol,path,model,request_json,{header_col},started_at,status,finished_at FROM {requests_table}"
                )
        if responses_table:
            columns = self._columns(responses_table)
            if {"request_id", "status_code", "response_json", "raw_body", "error"} <= columns:
                status_expr = "'completed'"
                if requests_table:
                    status_expr = f"COALESCE((SELECT status FROM {requests_table} old_req WHERE old_req.request_id={responses_table}.request_id), 'completed')"
                self.db.execute(
                    f"INSERT OR IGNORE INTO responses "
                    f"(request_id,status_code,response_json,raw_body,response_id,previous_response_id,terminal_status,error) "
                    f"SELECT request_id,status_code,response_json,raw_body,"
                    f"json_extract(response_json, '$.id'),NULL,{status_expr},error FROM {responses_table}"
                )
        if events_table:
            columns = self._columns(events_table)
            if self._EVENT_COLUMNS <= columns:
                self.db.execute(
                    f"INSERT OR IGNORE INTO stream_events(request_id,sequence,event_name,data_json,raw_frame) "
                    f"SELECT request_id,sequence,event_name,data_json,raw_frame FROM {events_table}"
                )

    def close(self) -> None:
        """无额外输入，提交待写流事件并关闭连接，返回 None。

        有待写数据时执行 commit；只读连接不写入，之后不可继续使用此实例。
        强制杀进程不能依赖本方法完成提交。
        """
        with self.lock:
            if not self.read_only and self._pending_events:
                self.db.commit()
                self._pending_events = 0
            self.db.close()

    def start_request(self, request_id: str, run_id: str, protocol: str, path: str, model: str | None, body: dict[str, Any], headers: dict[str, str]) -> int:
        """写入 ``in_flight`` 请求并返回该 run 内递增序号。

        body/headers 会以 JSON 保存（调用方应先脱敏 headers）；此方法不保存
        上游响应，响应需由 ``finish`` 写入。序号通过 IMMEDIATE 写事务分配，
        返回成功时在同一 run 内唯一递增；其他连接持有写锁超过 busy timeout
        时仍会抛出 sqlite3.OperationalError。同步等待期间会阻塞调用线程。
        """
        with self.lock:
            # IMMEDIATE obtains the database write reservation before reading
            # MAX(sequence).  This makes allocation atomic across independent
            # worker processes, while the UNIQUE(run_id, sequence) index is a
            # final guard against unexpected legacy/schema races.
            if self._pending_events:
                self.db.commit()
                self._pending_events = 0
            self.db.execute("BEGIN IMMEDIATE")
            try:
                sequence = self.db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM requests WHERE run_id=?", (run_id,)).fetchone()[0]
                self.db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,NULL)", (request_id, run_id, sequence, protocol, path, model, json.dumps(body, ensure_ascii=False), json.dumps(headers, ensure_ascii=False), utc_now(), "in_flight"))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            self._pending_events = 0
            return int(sequence)

    def save_request_wire(self, request_id: str, request_body: bytes, forwarded_body: bytes) -> None:
        """以短事务保存接收/转发正文 bytes；输入已登记 ID，返回 None，不保存认证头。"""
        with self.lock, self.db:
            self.db.execute("INSERT INTO request_wire VALUES (?,?,?)", (request_id, request_body, forwarded_body))

    def _append_archive(self, request_id: str) -> None:
        """输入 request_id，查询该条记录并同步追加 JSONL；返回 None。

        bytes 编码为 base64 对象；没有记录或归档路径时不写。不会扫描所有历史，
        但追加仍会阻塞调用线程，OSError 由 finish 处理，不能保证镜像归档始终成功。
        """
        record = self._record(request_id)
        if record is None or self.raw_archive_path is None:
            return
        def encode(value: Any) -> Any:
            """递归输入记录值，输出可 JSON 编码的副本；bytes 使用带 base64 键的对象。

            不修改输入、不执行 I/O；这仅编码已有捕获，不能恢复没有保存的请求字节。
            """
            if isinstance(value, bytes):
                return {"base64": base64.b64encode(value).decode("ascii")}
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, list):
                return [encode(item) for item in value]
            return value
        with self.raw_archive_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(encode(record), ensure_ascii=False) + "\n")

    def add_event(self, request_id: str, sequence: int, event_name: str | None, data: dict[str, Any] | None, raw_frame: bytes) -> None:
        """输入 request_id、请求内序号、事件名、JSON 和原始帧，提交一个事件。
        返回 None 表示事务已提交；同序号替换。不保留跨网络等待的事务，数据库异常向上传播。"""
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO stream_events VALUES (?,?,?,?,?)", (request_id, sequence, event_name, json.dumps(data, ensure_ascii=False) if data is not None else None, raw_frame))
            self.db.commit()

    def finish(self, request_id: str, *, status: str, status_code: int | None, response: dict[str, Any] | None, raw_body: bytes | None, response_id: str | None, previous_response_id: str | None, error: str | None = None) -> None:
        """关闭请求并写入响应快照、关联 response IDs 与可选错误文本。

        输入 status 是网关捕获终态，status_code 是上游 HTTP 状态；response 是
        解析/组装后的对象，raw_body 是可选原始 bytes。提交数据库后可选追加归档，
        归档发生 OSError 不会撤销已经提交的 SQLite 记录；函数无返回值。
        """
        with self.lock, self.db:
            self.db.execute("UPDATE requests SET status=?, finished_at=? WHERE request_id=?", (status, utc_now(), request_id))
            self.db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?,?,?,?)", (request_id, status_code, json.dumps(response, ensure_ascii=False) if response is not None else None, raw_body, response_id, previous_response_id, status, error))
            self.db.commit()
            self._pending_events = 0
            if self.raw_archive_path:
                try:
                    self._append_archive(request_id)
                except OSError:
                    # SQLite remains the source of truth; an optional archive
                    # must not turn a successful upstream response into 500.
                    pass

    def records(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """输入可选 run_id，返回包含时间、序号、路径、bytes 和事件的一致读快照。
        所有查询位于同一只读事务中，排序按 requests.rowid；不代表跨任务的工具执行顺序。"""
        with self.lock:
            query = "SELECT r.*, p.status_code,p.response_json,p.raw_body,p.response_id,p.previous_response_id,p.terminal_status,p.error FROM requests r LEFT JOIN responses p ON p.request_id=r.request_id"
            args: tuple[Any, ...] = ()
            if run_id:
                query += " WHERE r.run_id=?"
                args = (run_id,)
            query += " ORDER BY r.rowid"
            self.db.execute("BEGIN")
            try:
                rows = self.db.execute(query, args).fetchall()
                return [self._row_to_record(row) for row in rows]
            finally:
                self.db.rollback()

    def _row_to_record(self, row: sqlite3.Row) -> dict[str, Any]:
        """输入联表 SQLite Row，输出解析记录及有序 SSE 事件。
        补充请求/帧序号和时间，v3 请求 bytes 可用则读取，否则明确返回 None。无 schema 修改。"""
        events = self.db.execute("SELECT * FROM stream_events WHERE request_id=? ORDER BY sequence", (row["request_id"],)).fetchall()
        wire = None
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='request_wire'").fetchone():
            wire = self.db.execute("SELECT request_body,forwarded_body FROM request_wire WHERE request_id=?", (row["request_id"],)).fetchone()
        return {"request_body": wire[0] if wire else None, "forwarded_body": wire[1] if wire else None, "wire_bytes_available": wire is not None, "path": row["path"], "sequence": row["sequence"], "started_at": row["started_at"], "finished_at": row["finished_at"], "request_id": row["request_id"], "run_id": row["run_id"], "protocol": row["protocol"], "model": row["model"], "request": json.loads(row["request_json"]), "request_headers": json.loads(row["headers_json"]), "status": row["status"], "status_code": row["status_code"], "response": json.loads(row["response_json"]) if row["response_json"] else None, "raw_body": row["raw_body"], "response_id": row["response_id"], "previous_response_id": row["previous_response_id"], "terminal_status": row["terminal_status"], "error": row["error"], "events": [{"sequence": e["sequence"], "event": e["event_name"], "data": json.loads(e["data_json"]) if e["data_json"] else None, "raw": e["raw_frame"]} for e in events]}

    def _record(self, request_id: str) -> dict[str, Any] | None:
        """Read one joined record without scanning the complete database."""
        with self.lock:
            row = self.db.execute(
                "SELECT r.*, p.status_code,p.response_json,p.raw_body,p.response_id,p.previous_response_id,p.terminal_status,p.error "
                "FROM requests r LEFT JOIN responses p ON p.request_id=r.request_id WHERE r.request_id=?",
                (request_id,),
            ).fetchone()
            return self._row_to_record(row) if row is not None else None
