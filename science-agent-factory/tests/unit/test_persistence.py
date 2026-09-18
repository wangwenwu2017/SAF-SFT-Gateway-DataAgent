"""工作流原子状态存储与单写者锁的契约测试。

测试目的：确认共享持久化实现保存前校验、原子替换、失败清理和非阻塞互斥行为。
被测接口：AtomicJsonStore、JobFileLock，以及 Job、Batch 的类型化存储。
输入与夹具：临时目录、最小 Pydantic 状态、固定时钟以及 fsync/replace 故障注入。
预期结果：合法状态可往返读写；失败不覆盖已存状态且不遗留临时文件；锁可在退出后重用；
两种包装存储保留各自状态文件和锁路径。测试只写临时文件，不调用 SDK、Docker 或网络。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, ValidationError

from science_agent_factory.workflow import persistence
from science_agent_factory.workflow.batch import BatchStateStore
from science_agent_factory.workflow.persistence import AtomicJsonStore, JobFileLock
from science_agent_factory.workflow.state import JobStateStore


class _State(BaseModel):
    value: int = Field(ge=0)
    updated_at: datetime = datetime(2020, 1, 1, tzinfo=UTC)


class _PlainState(BaseModel):
    value: int


def test_create_load_and_save_round_trip(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path / "job", "state.json", _State)
    state = _State(value=1)

    assert store.create(state) is state
    assert store.load() == state
    assert store.path.read_text(encoding="utf-8").endswith("\n")

    state.value = 2
    store.save(state)

    assert store.load() == state
    assert store.load().value == 2
    assert not list(store.root.glob("*.tmp"))


def test_create_rejects_existing_state_without_overwriting(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path, "state.json", _State)
    store.create(_State(value=1))
    original = store.path.read_bytes()

    with pytest.raises(FileExistsError, match="state already exists"):
        store.create(_State(value=2))

    assert store.path.read_bytes() == original


def test_save_updates_timestamp_using_utc_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = datetime(2026, 9, 5, 6, 7, 8, tzinfo=UTC)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return expected

    monkeypatch.setattr(persistence, "datetime", _FixedDatetime)
    store = AtomicJsonStore(tmp_path, "state.json", _State)
    state = _State(value=1)

    store.save(state)

    assert state.updated_at == expected
    assert store.load().updated_at == expected


def test_save_supports_models_without_timestamp(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path, "plain.json", _PlainState)
    state = _PlainState(value=3)

    store.save(state)

    assert store.load() == state
    assert not hasattr(store.load(), "updated_at")


def test_invalid_state_does_not_overwrite_previous_file(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path, "state.json", _State)
    store.create(_State(value=1))
    original = store.path.read_bytes()
    invalid = _State(value=1).model_copy(update={"value": -1})

    with pytest.raises(ValidationError):
        store.save(invalid)

    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_failed_atomic_save_preserves_file_and_removes_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store = AtomicJsonStore(tmp_path, "state.json", _State)
    store.create(_State(value=1))
    original = store.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError(f"injected {operation} failure")

    monkeypatch.setattr(persistence.os, operation, fail)

    with pytest.raises(OSError, match=f"injected {operation} failure"):
        store.save(_State(value=2))

    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_load_rejects_invalid_serialized_state(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path, "state.json", _State)
    store.path.write_text('{"value": -1}', encoding="utf-8")

    with pytest.raises(ValidationError):
        store.load()


def test_load_missing_state_raises_file_not_found(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path, "state.json", _State)

    with pytest.raises(FileNotFoundError):
        store.load()


def test_file_lock_rejects_concurrent_writer_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "job" / ".job.lock"
    competing = JobFileLock(path)

    with JobFileLock(path) as acquired:
        assert acquired.path == path
        with pytest.raises(RuntimeError, match="job is already running: job"), competing:
            pytest.fail("competing writer acquired the held lock")

    with competing:
        assert path.is_file()


def test_file_lock_releases_after_body_failure(tmp_path: Path) -> None:
    path = tmp_path / ".job.lock"

    with pytest.raises(ValueError, match="injected body failure"), JobFileLock(path):
        raise ValueError("injected body failure")

    with JobFileLock(path):
        assert path.exists()


@pytest.mark.parametrize(
    ("store_type", "filename", "lock_filename", "root_attribute"),
    [
        (JobStateStore, "state.json", ".job.lock", "job_dir"),
        (BatchStateStore, "batch-state.json", ".batch.lock", "batch_dir"),
    ],
)
def test_typed_stores_preserve_state_and_lock_paths(
    tmp_path: Path,
    store_type,
    filename: str,
    lock_filename: str,
    root_attribute: str,
) -> None:
    root = tmp_path / "nested" / "job"
    store = store_type(root)

    assert isinstance(store, AtomicJsonStore)
    assert store.path == root.resolve() / filename
    assert store.lock_path == root.resolve() / lock_filename
    assert getattr(store, root_attribute) == root.resolve()
    with store.lock() as acquired:
        assert acquired.path == store.lock_path
        with pytest.raises(RuntimeError, match="job is already running"), store_type(root).lock():
            pytest.fail("typed store used a different lock path")
