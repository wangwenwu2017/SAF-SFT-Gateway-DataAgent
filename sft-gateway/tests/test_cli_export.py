"""导出文件格式回归。输入为临时输出路径和构造 JSON 行；输出为 JSONL/缩进文件断言，不验证 CLI 原子发布、重跑残留或子进程退出码。"""

import json

from sft_gateway.cli import _write_jsonl, _write_pretty_json


def test_jsonl_remains_one_record_per_line_and_pretty_json_is_readable(tmp_path):
    rows = [
        {"task": "task-a", "tools": "[]", "messages": [{"role": "user", "content": "长文本"}]},
        {"task": "task-b", "tools": "[]", "messages": []},
    ]
    jsonl = tmp_path / "sft_samples.jsonl"
    pretty = tmp_path / "sft_samples.pretty.json"
    _write_jsonl(jsonl, rows)
    _write_pretty_json(pretty, rows)

    assert [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()] == rows
    pretty_text = pretty.read_text(encoding="utf-8")
    assert "\n  {" in pretty_text
    assert json.loads(pretty_text) == rows

