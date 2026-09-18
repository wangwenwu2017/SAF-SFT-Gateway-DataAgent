"""制品摘要的文件系统边界测试。

测试目的：缺失路径和符号链接不能产生看似有效的缓存摘要。
被测接口：artifact_digest、tree_digest。
输入与夹具：临时普通文件、目录、缺失路径与符号链接。
预期结果：合法内容稳定；类型、存在性和符号链接错误显式失败。
"""

import pytest

from science_agent_factory.storage.digests import artifact_digest, tree_digest


def test_digest_requires_existing_artifact(tmp_path):
    with pytest.raises(FileNotFoundError):
        artifact_digest(tmp_path / "missing")


def test_tree_digest_requires_directory(tmp_path):
    file = tmp_path / "file"
    file.write_text("content")
    with pytest.raises(NotADirectoryError):
        tree_digest(file)


@pytest.mark.parametrize("directory", [False, True])
def test_digest_rejects_symlink_root(tmp_path, directory):
    source = tmp_path / "source"
    if directory:
        source.mkdir()
    else:
        source.write_text("content")
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        artifact_digest(alias)


def test_digest_is_stable_and_content_sensitive(tmp_path):
    directory = tmp_path / "tree"
    directory.mkdir()
    (directory / "file").write_text("one")
    before = tree_digest(directory)
    assert artifact_digest(directory) == before
    (directory / "file").write_text("two")
    assert tree_digest(directory) != before
