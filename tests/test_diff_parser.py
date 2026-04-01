import pytest
from diffgraph.diff_parser import parse_diff


SIMPLE_DIFF = """\
diff --git a/src/PaymentService.java b/src/PaymentService.java
index 1234567..abcdefg 100644
--- a/src/PaymentService.java
+++ b/src/PaymentService.java
@@ -10,6 +10,8 @@
 context line
-    old code
+    new code
+    extra line
 context line
"""

NEW_FILE_DIFF = """\
diff --git a/src/NewClass.java b/src/NewClass.java
new file mode 100644
index 0000000..abcdefg
--- /dev/null
+++ b/src/NewClass.java
@@ -0,0 +1,3 @@
+public class NewClass {
+    void foo() {}
+}
"""

DELETED_FILE_DIFF = """\
diff --git a/src/OldClass.java b/src/OldClass.java
deleted file mode 100644
index abcdefg..0000000
--- a/src/OldClass.java
+++ /dev/null
@@ -1,3 +0,0 @@
-public class OldClass {
-    void bar() {}
-}
"""

RENAMED_FILE_DIFF = """\
diff --git a/src/Old.java b/src/New.java
similarity index 90%
rename from src/Old.java
rename to src/New.java
--- a/src/Old.java
+++ b/src/New.java
@@ -1,3 +1,3 @@
 public class New {
-    void oldMethod() {}
+    void newMethod() {}
 }
"""

MULTI_HUNK_DIFF = """\
diff --git a/service.py b/service.py
index 1111111..2222222 100644
--- a/service.py
+++ b/service.py
@@ -5,4 +5,3 @@
 line5
-line6
 line7
 line8
@@ -20,3 +19,4 @@
 line20
+new_line
 line21
 line22
"""


class TestSimpleDiff:
    def setup_method(self):
        self.result = parse_diff(SIMPLE_DIFF)

    def test_one_file(self):
        assert len(self.result.files) == 1

    def test_path(self):
        assert "src/PaymentService.java" in self.result.files

    def test_status_modified(self):
        assert self.result.files["src/PaymentService.java"].status == "modified"

    def test_changed_files_includes_modified(self):
        assert "src/PaymentService.java" in self.result.changed_files

    def test_hunk_count(self):
        fd = self.result.files["src/PaymentService.java"]
        assert len(fd.hunks) == 1

    def test_hunk_before_start(self):
        hunk = self.result.files["src/PaymentService.java"].hunks[0]
        assert hunk.before_start == 10

    def test_hunk_after_start(self):
        hunk = self.result.files["src/PaymentService.java"].hunks[0]
        assert hunk.after_start == 10

    def test_before_lines(self):
        hunk = self.result.files["src/PaymentService.java"].hunks[0]
        assert hunk.before_lines == ["    old code"]

    def test_after_lines(self):
        hunk = self.result.files["src/PaymentService.java"].hunks[0]
        assert hunk.after_lines == ["    new code", "    extra line"]

    def test_after_changed_lines(self):
        # context=line10, then +new_code=line11, +extra=line12
        fd = self.result.files["src/PaymentService.java"]
        assert fd.after_changed_lines == [11, 12]

    def test_changed_lines_dict(self):
        assert self.result.changed_lines["src/PaymentService.java"] == [11, 12]


class TestNewFile:
    def setup_method(self):
        self.result = parse_diff(NEW_FILE_DIFF)

    def test_status_added(self):
        assert self.result.files["src/NewClass.java"].status == "added"

    def test_before_path_none(self):
        assert self.result.files["src/NewClass.java"].before_path is None

    def test_in_changed_files(self):
        assert "src/NewClass.java" in self.result.changed_files

    def test_all_lines_are_after_changed(self):
        fd = self.result.files["src/NewClass.java"]
        assert fd.after_changed_lines == [1, 2, 3]

    def test_no_before_lines(self):
        hunk = self.result.files["src/NewClass.java"].hunks[0]
        assert hunk.before_lines == []


class TestDeletedFile:
    def setup_method(self):
        self.result = parse_diff(DELETED_FILE_DIFF)

    def test_status_deleted(self):
        assert self.result.files["src/OldClass.java"].status == "deleted"

    def test_not_in_changed_files(self):
        assert "src/OldClass.java" not in self.result.changed_files

    def test_no_after_changed_lines(self):
        fd = self.result.files["src/OldClass.java"]
        assert fd.after_changed_lines == []


class TestRenamedFile:
    def setup_method(self):
        self.result = parse_diff(RENAMED_FILE_DIFF)

    def test_status_renamed(self):
        fd = self.result.files.get("src/New.java")
        assert fd is not None
        assert fd.status == "renamed"

    def test_in_changed_files(self):
        assert "src/New.java" in self.result.changed_files


class TestMultiHunk:
    def setup_method(self):
        self.result = parse_diff(MULTI_HUNK_DIFF)
        self.fd = self.result.files["service.py"]

    def test_two_hunks(self):
        assert len(self.fd.hunks) == 2

    def test_hunk1_before_lines(self):
        assert self.fd.hunks[0].before_lines == ["line6"]

    def test_hunk1_after_lines_empty(self):
        assert self.fd.hunks[0].after_lines == []

    def test_hunk2_after_lines(self):
        assert self.fd.hunks[1].after_lines == ["new_line"]

    def test_hunk2_after_changed_line(self):
        # hunk2: after_start=19, context(20), then +new_line(20)
        assert 20 in self.fd.after_changed_lines


class TestEmptyDiff:
    def test_empty_string(self):
        result = parse_diff("")
        assert result.files == {}
        assert result.changed_files == []

    def test_no_hunks_diff(self):
        diff = "diff --git a/README.md b/README.md\n"
        result = parse_diff(diff)
        # Parses the file but has no hunks
        assert "README.md" in result.files
        assert result.files["README.md"].hunks == []
