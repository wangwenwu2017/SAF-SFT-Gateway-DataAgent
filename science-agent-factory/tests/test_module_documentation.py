"""全仓库 Python 模块文件头完整性测试。

测试目的：防止新增或重构代码时遗漏模块用途、接口、输入输出和边界说明。
被测接口：Python AST 模块 docstring，以及 ``src``/``scripts``/``tests``、Shell 脚本和 Git Hook 的约定章节结构。
输入与夹具：项目生产包、脚本、Hook 和测试代码，不读取缓存、虚拟环境或生成目录。
预期结果：生产与测试 Python 模块具备约定章节且可解析，Shell 代码头具备五个生产章节。
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
SCRIPT_ROOT = PROJECT_ROOT / "scripts"
TEST_ROOT = PROJECT_ROOT / "tests"
GIT_HOOK_ROOT = PROJECT_ROOT / ".githooks"
SOURCE_SECTIONS = ("模块用途", "主要接口", "输入", "输出", "副作用与边界")
TEST_SECTIONS = ("测试目的", "被测接口", "输入与夹具", "预期结果")


def test_all_python_modules_have_structured_headers() -> None:
    problems: list[str] = []
    for root, required_sections in (
        (SOURCE_ROOT, SOURCE_SECTIONS),
        (SCRIPT_ROOT, SOURCE_SECTIONS),
        (TEST_ROOT, TEST_SECTIONS),
    ):
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(PROJECT_ROOT)
            try:
                module = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
            except SyntaxError as exc:
                problems.append(f"{relative}: 无法解析模块文件头（{exc.msg}）")
                continue
            docstring = ast.get_docstring(module, clean=False)
            if not docstring:
                problems.append(f"{relative}: 缺少模块级 docstring")
                continue
            missing = [section for section in required_sections if section not in docstring]
            if missing:
                problems.append(f"{relative}: 缺少章节 {', '.join(missing)}")

    assert not problems, "模块文件头不完整：\n" + "\n".join(problems)


def test_all_shell_modules_have_structured_headers() -> None:
    problems: list[str] = []
    paths = sorted(SCRIPT_ROOT.rglob("*.sh"))
    if GIT_HOOK_ROOT.is_dir():
        paths.extend(sorted(path for path in GIT_HOOK_ROOT.rglob("*") if path.is_file()))
    for path in paths:
        relative = path.relative_to(PROJECT_ROOT)
        header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:12])
        missing = [section for section in SOURCE_SECTIONS if section not in header]
        if missing:
            problems.append(f"{relative}: 缺少章节 {', '.join(missing)}")

    assert not problems, "Shell 文件头不完整：\n" + "\n".join(problems)
