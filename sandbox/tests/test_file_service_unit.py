import os

import pytest

from app.core.exceptions import BadRequestException
from app.services.file import FileService


@pytest.mark.asyncio
async def test_recursive_find_rejects_filesystem_root():
    service = FileService()

    with pytest.raises(BadRequestException, match="root-level"):
        await service.find_by_name("/tmp/../..", "**/PLAN.md")

    with pytest.raises(BadRequestException, match="root-level"):
        await service.find_by_name("/", "*/*/PLAN.md")


@pytest.mark.asyncio
async def test_non_recursive_find_allows_filesystem_root():
    service = FileService()

    result = await service.find_by_name("/", "definitely-not-present-*.txt")

    assert result.path == "/"
    assert result.files == []


@pytest.mark.asyncio
async def test_find_rejects_absolute_and_parent_traversal_globs(tmp_path):
    service = FileService()

    for pattern in ("/**/PLAN.md", "\\**\\PLAN.md", "../**/PLAN.md"):
        with pytest.raises(BadRequestException, match="relative path"):
            await service.find_by_name(str(tmp_path), pattern)


@pytest.mark.asyncio
async def test_recursive_find_does_not_follow_directory_symlinks(tmp_path):
    service = FileService()
    search_root = tmp_path / "search"
    outside_root = tmp_path / "outside"
    search_root.mkdir()
    outside_root.mkdir()
    (outside_root / "secret.md").write_text("secret")
    (search_root / "outside-link").symlink_to(
        outside_root,
        target_is_directory=True,
    )

    result = await service.find_by_name(str(search_root), "**/secret.md")

    assert result.files == []


@pytest.mark.asyncio
async def test_recursive_find_matches_zero_or_more_directories(tmp_path):
    service = FileService()
    nested = tmp_path / "nested"
    nested.mkdir()
    root_file = tmp_path / "PLAN.md"
    nested_file = nested / "PLAN.md"
    root_file.write_text("root")
    nested_file.write_text("nested")

    result = await service.find_by_name(str(tmp_path), "**/PLAN.md")

    assert set(result.files) == {str(root_file), str(nested_file)}


@pytest.mark.asyncio
async def test_find_preserves_glob_hidden_file_rules(tmp_path):
    service = FileService()
    visible = tmp_path / "visible.md"
    hidden = tmp_path / ".hidden.md"
    visible.write_text("visible")
    hidden.write_text("hidden")

    ordinary_result = await service.find_by_name(str(tmp_path), "**/*.md")
    hidden_result = await service.find_by_name(str(tmp_path), "**/.hidden.md")

    assert ordinary_result.files == [str(visible)]
    assert hidden_result.files == [str(hidden)]


@pytest.mark.asyncio
async def test_find_prunes_hidden_directories(monkeypatch, tmp_path):
    service = FileService()
    search_root = str(tmp_path)

    def controlled_walk(path, followlinks=False):
        directories = [".cache", "project"]
        yield search_root, directories, []
        assert ".cache" not in directories
        yield os.path.join(search_root, "project"), [], ["report.md"]

    monkeypatch.setattr("app.services.file.os.walk", controlled_walk)

    result = await service.find_by_name(search_root, "**/*.md")

    assert result.files == [str(tmp_path / "project" / "report.md")]


@pytest.mark.asyncio
async def test_find_normalizes_current_directory_segments(tmp_path):
    service = FileService()
    nested = tmp_path / "nested"
    nested.mkdir()
    root_file = tmp_path / "root.md"
    nested_file = nested / "nested.md"
    root_file.write_text("root")
    nested_file.write_text("nested")

    root_result = await service.find_by_name(str(tmp_path), "./*.md")
    nested_result = await service.find_by_name(str(tmp_path), "nested/./*.md")

    assert root_result.files == [str(root_file)]
    assert nested_result.files == [str(nested_file)]


@pytest.mark.asyncio
async def test_find_trailing_slash_matches_directories_only(tmp_path):
    service = FileService()
    directory = tmp_path / "folder"
    file_path = tmp_path / "file.txt"
    directory.mkdir()
    file_path.write_text("file")

    result = await service.find_by_name(str(tmp_path), "*/")

    assert result.files == [f"{directory}{os.path.sep}"]
