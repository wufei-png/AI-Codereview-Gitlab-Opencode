import pytest

from biz.agent.safety import SENSITIVE_PATH_PATTERNS  # noqa: F401  ensure import
from biz.agent.tools.read_file import ReadFileTool


@pytest.fixture
def repo_with_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text(
        "line1\nline2\nline3\nline4\nline5\n"
    )
    (repo / ".env").write_text("SECRET=x\n")
    binary = repo / "blob.bin"
    binary.write_bytes(b"\x00\x01\x00\x02\x00\x03")
    return repo


class TestReadFile:
    @pytest.mark.parametrize("character", ["é", "中", "😀"])
    def test_utf8_character_crossing_sample_boundary(self, tmp_path, character):
        before = "before\n"
        long_line = "x" * (8191 - len(before)) + character + " tail"
        content = before + long_line + "\nafter\n"
        (tmp_path / "text.txt").write_bytes(content.encode("utf-8"))

        tool = ReadFileTool(tmp_path)
        full = tool.execute(path="text.txt")
        assert full.success is True
        assert full.error is None
        assert full.output == f"lines 1-3:\n{before}{long_line}\nafter"

        page = tool.execute(path="text.txt", offset=1, limit=1)
        assert page.success is True
        assert page.error is None
        assert page.output == f"lines 2-2:\n{long_line}"

    def test_utf8_character_ending_at_sample_boundary(self, tmp_path):
        long_line = "x" * 8190 + "é" + " tail"
        (tmp_path / "text.txt").write_bytes((long_line + "\n").encode("utf-8"))

        result = ReadFileTool(tmp_path).execute(path="text.txt")
        assert result.success is True
        assert result.error is None
        assert result.output == f"lines 1-1:\n{long_line}"

    @pytest.mark.parametrize("line_ending", ["\n", "\r\n", ""])
    def test_ascii_line_endings_and_empty_file(self, tmp_path, line_ending):
        file = tmp_path / "text.txt"
        file.write_bytes(("first" + line_ending + "second").encode("utf-8"))

        result = ReadFileTool(tmp_path).execute(path="text.txt")
        assert result.success is True
        assert result.error is None
        expected = "first\nsecond" if line_ending else "firstsecond"
        count = 2 if line_ending else 1
        assert result.output == f"lines 1-{count}:\n{expected}"

        file.write_bytes(b"")
        empty = ReadFileTool(tmp_path).execute(path="text.txt")
        assert empty.success is True
        assert empty.error is None
        assert empty.output == "lines 1-0:\n"

    def test_crlf_crossing_sample_boundary(self, tmp_path):
        first_line = "x" * 8191
        content = first_line + "\r\nsecond\r\n"
        (tmp_path / "text.txt").write_bytes(content.encode("utf-8"))

        result = ReadFileTool(tmp_path).execute(path="text.txt")
        assert result.success is True
        assert result.error is None
        assert result.output == f"lines 1-2:\n{first_line}\nsecond"

    def test_invalid_utf8_is_replaced(self, tmp_path):
        (tmp_path / "text.txt").write_bytes(b"before\xffafter\n")

        result = ReadFileTool(tmp_path).execute(path="text.txt")
        assert result.success is True
        assert result.error is None
        assert result.output == "lines 1-1:\nbefore\ufffdafter"

    def test_read_full_file_default(self, repo_with_files):
        t = ReadFileTool(repo_with_files)
        r = t.execute(path="src/main.py")
        assert r.success is True
        assert "line1" in r.output
        assert "line5" in r.output

    def test_read_with_offset_and_limit(self, repo_with_files):
        t = ReadFileTool(repo_with_files)
        r = t.execute(path="src/main.py", offset=2, limit=2)
        assert r.success is True
        # Format: "lines N-M:\n<content>"
        assert "line3" in r.output
        assert "line4" in r.output
        assert "line5" not in r.output

    def test_path_outside_repo_rejected(self, repo_with_files):
        t = ReadFileTool(repo_with_files)
        r = t.execute(path="../escape.txt")
        assert r.success is False
        assert "outside repo" in (r.error or "").lower()

    def test_absolute_path_outside_repo_rejected(self, repo_with_files):
        t = ReadFileTool(repo_with_files)
        r = t.execute(path="/etc/passwd")
        assert r.success is False
        assert "outside repo" in (r.error or "").lower()

    def test_sensitive_path_rejected(self, repo_with_files):
        t = ReadFileTool(repo_with_files)
        r = t.execute(path=".env")
        assert r.success is False
        assert "sensitive" in (r.error or "").lower()

    def test_binary_file_rejected(self, repo_with_files):
        t = ReadFileTool(repo_with_files)
        r = t.execute(path="blob.bin")
        assert r.success is False
        assert "binary" in (r.error or "").lower()

    def test_missing_file(self, repo_with_files):
        t = ReadFileTool(repo_with_files)
        r = t.execute(path="nonexistent.py")
        assert r.success is False
        assert "not found" in (r.error or "").lower()

    def test_to_schema_has_path_param(self, repo_with_files):
        t = ReadFileTool(repo_with_files)
        s = t.to_schema()
        assert s["function"]["name"] == "read_file"
        assert "path" in s["function"]["parameters"]["properties"]
