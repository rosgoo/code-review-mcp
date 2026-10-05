import hashlib

import pytest

from code_review_mcp.reverse_patch import reconstruct_old_content, split_file_patches

OLD = "".join(f"line {n}\n" for n in range(1, 21))


def _patch(*hunks: str, header: str = "diff --git a/f.txt b/f.txt") -> str:
    return "\n".join([header, "--- a/f.txt", "+++ b/f.txt", *hunks]) + "\n"


def test_added_lines() -> None:
    new = OLD.replace("line 3\n", "line 3\nadded a\nadded b\n")
    patch = _patch("@@ -2,3 +2,5 @@", " line 2", " line 3", "+added a", "+added b", " line 4")

    assert reconstruct_old_content(new, patch) == OLD


def test_deleted_lines() -> None:
    new = OLD.replace("line 5\nline 6\n", "")
    patch = _patch("@@ -4,4 +4,2 @@", " line 4", "-line 5", "-line 6", " line 7")

    assert reconstruct_old_content(new, patch) == OLD


def test_several_hunks() -> None:
    new = OLD.replace("line 2\n", "line two\n").replace("line 18\n", "")
    patch = _patch(
        "@@ -1,3 +1,3 @@",
        " line 1",
        "-line 2",
        "+line two",
        " line 3",
        "@@ -17,3 +17,2 @@",
        " line 17",
        "-line 18",
        " line 19",
    )

    assert reconstruct_old_content(new, patch) == OLD


def test_new_file() -> None:
    patch = _patch("@@ -0,0 +1,2 @@", "+a", "+b", header="diff --git a/new.txt b/new.txt")

    assert reconstruct_old_content("a\nb\n", patch) == ""


def test_deleted_file() -> None:
    patch = _patch("@@ -1,2 +0,0 @@", "-a", "-b")

    assert reconstruct_old_content("", patch) == "a\nb\n"


def test_zero_context_deletion_after_a_line() -> None:
    new = OLD.replace("line 10\n", "")
    patch = _patch("@@ -10 +9,0 @@", "-line 10")

    assert reconstruct_old_content(new, patch) == OLD


@pytest.mark.parametrize(
    "new",
    [
        pytest.param(OLD.replace("line 3\n", "line 3\nadded a\nadded X\n"), id="added-text"),
        pytest.param(OLD.replace("line 2\n", "line 2 edited\n"), id="context-text"),
        pytest.param("line 1\nline 2\n", id="file-too-short"),
    ],
)
def test_mismatch_returns_none(new: str) -> None:
    patch = _patch("@@ -2,3 +2,5 @@", " line 2", " line 3", "+added a", "+added b", " line 4")

    assert reconstruct_old_content(new, patch) is None


def test_overlapping_hunks_return_none() -> None:
    patch = _patch("@@ -1,2 +1,2 @@", " line 1", " line 2", "@@ -1,1 +1,1 @@", " line 1")

    assert reconstruct_old_content(OLD, patch) is None


def test_truncated_hunk_returns_none() -> None:
    patch = _patch("@@ -1,3 +1,3 @@", " line 1")

    assert reconstruct_old_content(OLD, patch) is None


def test_binary_file_returns_none() -> None:
    patch = "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"

    assert reconstruct_old_content("��", patch) is None


def test_rename_without_hunks_keeps_content() -> None:
    patch = (
        "diff --git a/a.txt b/b.txt\nsimilarity index 100%\nrename from a.txt\nrename to b.txt\n"
    )

    assert reconstruct_old_content("same\n", patch) == "same\n"


def test_newline_added_at_end_of_file() -> None:
    patch = _patch("@@ -1,2 +1,2 @@", " a", "-b", "\\ No newline at end of file", "+b")

    assert reconstruct_old_content("a\nb\n", patch) == "a\nb"


def test_newline_removed_at_end_of_file() -> None:
    patch = _patch("@@ -1,2 +1,2 @@", " a", "-b", "+b", "\\ No newline at end of file")

    assert reconstruct_old_content("a\nb", patch) == "a\nb\n"


def test_no_newline_on_unchanged_last_line() -> None:
    patch = _patch("@@ -1,2 +1,2 @@", "-a", "+A", " b", "\\ No newline at end of file")

    assert reconstruct_old_content("A\nb", patch) == "a\nb"


def test_end_of_file_newline_mismatch_returns_none() -> None:
    patch = _patch("@@ -1,2 +1,2 @@", " a", "-b", "+b", "\\ No newline at end of file")

    assert reconstruct_old_content("a\nb\n", patch) is None


def test_split_file_patches_keys_by_new_path() -> None:
    first = _patch("@@ -1 +1 @@", "-a", "+b", header="diff --git a/old.py b/new.py")
    second = _patch("@@ -1 +1 @@", "-c", "+d", header="diff --git a/x/y.md b/x/y.md")

    sections = split_file_patches(first + second)

    assert list(sections) == ["new.py", "x/y.md"]
    assert sections["new.py"].startswith("diff --git a/old.py b/new.py\n")
    assert "+d" in sections["x/y.md"]
    assert "+d" not in sections["new.py"]
    assert reconstruct_old_content("b\n", sections["new.py"]) == "a\n"


def _blob_id(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()[:7]


NEW_FILE_PATCH = (
    "diff --git a/n.py b/n.py\nnew file mode 100644\nindex 0000000..{new}\n"
    "--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
)


def test_lines_appended_after_a_new_file_hunk_return_none() -> None:
    patch = NEW_FILE_PATCH.replace("index 0000000..{new}\n", "")

    assert reconstruct_old_content("a\nb\n", patch) == ""
    assert reconstruct_old_content("a\nb\nappended\n", patch) is None


def test_deleted_file_with_content_on_disk_returns_none() -> None:
    patch = "diff --git a/d.py b/d.py\n--- a/d.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"

    assert reconstruct_old_content("", patch) == "x\n"
    assert reconstruct_old_content("x\n", patch) is None


def test_zero_context_insertion_at_top_keeps_the_rest() -> None:
    patch = _patch("@@ -0,0 +1 @@", "+first")

    assert reconstruct_old_content("first\n" + OLD, patch) == OLD


def test_index_blob_ids_are_checked() -> None:
    new = OLD.replace("line 2\n", "line two\n")
    hunk = ("@@ -1,3 +1,3 @@", " line 1", "-line 2", "+line two", " line 3")
    index = f"index {_blob_id(OLD)}..{_blob_id(new)} 100644"
    patch = _patch(index, *hunk)
    edited_outside_hunk = new.replace("line 20\n", "line 20 edited\n")
    wrong_old_id = _patch(f"index 1234567..{_blob_id(new)} 100644", *hunk)

    assert reconstruct_old_content(new, patch) == OLD
    assert reconstruct_old_content(edited_outside_hunk, patch) is None
    assert reconstruct_old_content(new, wrong_old_id) is None
    assert reconstruct_old_content("a\nb\n", NEW_FILE_PATCH.format(new=_blob_id("a\nb\n"))) == ""
