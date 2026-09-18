"""CLI 入口。输入 argv，serve 启动服务，export 只读捕获并发布 manifest，run 启动 harness。
导入无数据库写入；导出使用 base64 保存 bytes，正常与拒绝样本分开。"""

from __future__ import annotations

import argparse
import fcntl
from contextlib import contextmanager
import json
import hashlib
import tempfile
import uuid
import os
import subprocess
from pathlib import Path
from typing import Any

from .app import _build_export, _json_record, make_app
from .export.messages import export_samples, export_trajectories
from .export.codex import export_codex_samples
from .export.eligibility import rejection_reasons
from .gateway.session import new_run_id, validate_run_id
from .gateway.storage import Storage


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write one complete JSON value per line.

    JSONL is deliberately compact and line-oriented so it can be streamed by
    dataset tooling.  The companion pretty JSON export below is the
    human-maintainable representation; do not put indentation into this file,
    as that would make a single sample span multiple JSONL records.
    """
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_pretty_json(path: Path, value: Any) -> None:
    """Write a UTF-8, indented JSON document with a final newline."""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@contextmanager
def _export_lock(destination: Path):
    """串行化同一目录的发布；输入目标目录，输出上下文，竞争写入者明确失败。"""
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / ".export.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another export is writing this output directory") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _message_provenance(trajectory: Any) -> list[dict[str, Any]]:
    """把投影消息位置映射到叶 item 与首个观测请求；输入轨迹，输出审计映射。"""
    from .export.codex import _item_messages, _chat_messages
    from .gateway.detection import Protocol
    leaf = trajectory.turns[-1]
    project = _item_messages if leaf.protocol == Protocol.RESPONSES else _chat_messages
    mapping = []
    for item_index, item in enumerate(leaf.context + leaf.target):
        source = None
        for turn in trajectory.turns:
            start = len(turn.context)
            if start <= item_index < start + len(turn.target):
                source = {"request_id": turn.request_id, "field": "target", "item_index": item_index - start}
                break
            if item_index < start and source is None:
                source = {"request_id": turn.request_id, "field": "effective_context", "item_index": item_index}
        for part_index, _ in enumerate(project([item])):
            mapping.append({"message_index": len(mapping), "leaf_item_index": item_index,
                            "projected_part_index": part_index, "source": source})
    return mapping


def main(argv: list[str] | None = None) -> int:
    """解析 argv 并返回真实退出码。
    serve 构造应用；export 只读数据库，在临时目录生成、逐文件发布并最后写带校验和的 manifest。
    消费者须校验 manifest；同一输出目录仅允许一个导出写入者。run 覆盖 OPENAI_BASE_URL
    并传播子进程退出码。I/O 和参数错误向上传播，失败的临时目录可供诊断。"""
    parser = argparse.ArgumentParser(description="Codex black-box SFT gateway")
    sub = parser.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8100)
    serve.add_argument("--upstream")
    serve.add_argument("--db")
    export = sub.add_parser("export")
    export.add_argument("run_id")
    export.add_argument("--db", default=os.getenv("SFT_DB", "data/gateway.db"))
    export.add_argument("--output", default="dataset")
    export.add_argument("--task", default=None, help="task name in the Codex sample schema")
    export.add_argument("--include-incomplete", action="store_true", help="also write report-only samples ending with an unreturned tool call")
    run = sub.add_parser("run")
    run.add_argument("--gateway", default="http://127.0.0.1:8100")
    run.add_argument("--run-id", default=None)
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action == "serve":
        import uvicorn
        uvicorn.run(make_app(args.upstream, args.db), host=args.host, port=args.port)
        return 0
    if args.action == "export":
        # A live harness may still be writing this database.  Export must not
        # run schema migration (which takes a SQLite schema lock), so open it
        # through SQLite's mode=ro URI and let the writer remain uninterrupted.
        storage = Storage(args.db, read_only=True)
        try:
            raw_records = storage.records(validate_run_id(args.run_id))
            trajectories, stats = _build_export(raw_records)
        finally:
            storage.close()
        with _export_lock(Path(args.output)):
            destination = Path(args.output)
            destination.mkdir(parents=True, exist_ok=True)
            output = Path(tempfile.mkdtemp(prefix=".export-", dir=destination))
            trajectory_rows = list(export_trajectories(trajectories))
            sample_rows = list(export_samples(trajectories))
            sft_rows = list(export_codex_samples(trajectories, task=args.task))
            # The opt-in report is intentionally broad: it includes broken links
            # as well as unresolved tool calls, while the normal SFT export stays
            # strict and excludes both categories.
            incomplete_rows = list(export_codex_samples([t for t in trajectories if rejection_reasons(t)], task=args.task, include_incomplete=True, include_broken=True)) if args.include_incomplete else []
            stats["sft_trajectories"] = len(sft_rows)
            stats["sft_skipped_incomplete"] = max(0, len(trajectories) - len(sft_rows))
            _write_jsonl(output / "trajectories.messages.jsonl", trajectory_rows)
            _write_jsonl(output / "samples.messages.jsonl", sample_rows)
            request_by_id = {row["request_id"]: row for row in raw_records}
            native_rows = []
            for trajectory in trajectories:
                source_ids = [turn.request_id for turn in trajectory.turns]
                native_rows.append({
                    "trajectory_id": trajectory.trajectory_id,
                    "run_id": trajectory.run_id,
                    "protocol": trajectory.protocol.value,
                    "model": trajectory.model,
                    "break_reason": trajectory.break_reason,
                    "source_request_ids": source_ids,
                    "records": [_json_record(request_by_id[request_id]) for request_id in source_ids if request_id in request_by_id],
                })
            _write_jsonl(output / "trajectories.native.jsonl", native_rows)
            _write_jsonl(output / "sft_samples.jsonl", sft_rows)
            # Keep JSONL as the machine-facing stream format, while always
            # emitting one stable, readable aggregate for review/editing.  The
            # aggregate is an array even for one sample so its shape never
            # changes as a run gains another trajectory.
            _write_pretty_json(output / "sft_samples.pretty.json", sft_rows)
            _write_pretty_json(output / "sft_samples.json", sft_rows)
            if incomplete_rows:
                _write_jsonl(output / "sft_samples.incomplete.jsonl", incomplete_rows)
                _write_pretty_json(output / "sft_samples.incomplete.json", incomplete_rows)
                if len(incomplete_rows) == 1:
                    _write_pretty_json(output / "sample.incomplete.json", incomplete_rows[0])
            # Do not leave a stale single/multi-sample document behind when a
            # later export discovers that the only leaf ended on an unreturned
            # tool call.
            for stale in (output / "sample.json", output / "samples.json"):
                if stale.exists():
                    stale.unlink()
            if len(sft_rows) == 1:
                _write_pretty_json(output / "sample.json", sft_rows[0])
            elif sft_rows:
                _write_pretty_json(output / "samples.json", sft_rows)
            _write_jsonl(output / "raw_records.jsonl", [_json_record(row) for row in raw_records])
            provenance = []
            for trajectory in trajectories:
                leaf = trajectory.turns[-1]
                reasons = rejection_reasons(trajectory)
                provenance.append({
                    "trajectory_id": trajectory.trajectory_id,
                    "eligible": not reasons, "rejection_reasons": reasons,
                    "task_status": "unknown", "projection_version": "0.3.2",
                    "source_request_ids": [turn.request_id for turn in trajectory.turns],
                    "leaf_request_id": leaf.request_id,
                    "projection_source": "leaf.context + leaf.target in item order",
                    "message_sources": _message_provenance(trajectory),
                    "turns": [{"request_id": turn.request_id, "parent_request_id": turn.parent_request_id,
                               "link_method": turn.link_method, "prefix_length": turn.prefix_length,
                               "context_items": len(turn.context), "target_items": len(turn.target),
                               "effective_context": turn.context} for turn in trajectory.turns],
                    "projection_losses": ["developer mapped to system", "opaque reasoning omitted; readable summary is not full reasoning"],
                })
            _write_pretty_json(output / "provenance.json", provenance)
            stats["eligible"] = sum(row["eligible"] for row in provenance)
            stats["rejected"] = len(provenance) - stats["eligible"]
            stats["snapshot_request_ids"] = [row["request_id"] for row in raw_records]
            _write_pretty_json(output / "export_report.json", stats)
            # A manifest is published last. Consumers verify hashes before reading;
            # interruption cannot make mixed files appear to be a complete generation.
            hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in output.iterdir()}
            manifest = {"generation": uuid.uuid4().hex, "complete": True, "files": hashes}
            _write_pretty_json(output / "manifest.json", manifest)
            old_manifest = destination / "manifest.json"
            if old_manifest.exists():
                old_manifest.unlink()
            for name in ("sft_samples.incomplete.jsonl", "sft_samples.incomplete.json", "sample.incomplete.json", "sample.json", "samples.json"):
                stale = destination / name
                if stale.exists() and name not in hashes:
                    stale.unlink()
            for name in hashes:
                os.replace(output / name, destination / name)
            os.replace(output / "manifest.json", destination / "manifest.json")
            output.rmdir()
            return 0
    if not args.command:
        raise SystemExit("run requires a command")
    run_id = validate_run_id(args.run_id or new_run_id())
    env = os.environ.copy()
    env["X_SESSION_ID"] = run_id
    env["SFT_RUN_ID"] = run_id
    env["OPENAI_BASE_URL"] = f"{args.gateway.rstrip('/')}/runs/{run_id}/v1"
    return subprocess.run(args.command[1:] if args.command[:1] == ["--"] else args.command, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
