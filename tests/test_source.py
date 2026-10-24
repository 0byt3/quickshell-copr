import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qscopr.source import GateError, extract_source, select_release, should_build, version_key


class ReleaseTests(unittest.TestCase):
    def release(self, version="0.3.2", commit="a" * 40):
        return {
            "version": version,
            "commit": commit,
        }

    def test_numeric_version_order(self):
        self.assertGreater(version_key("v0.10.0"), version_key("v0.9.12"))

    def test_unsafe_or_prerelease_tag(self):
        for tag in ["v0.3.2-rc1", "../master", "v0.3", "master"]:
            with self.subTest(tag=tag), self.assertRaises(GateError):
                version_key(tag)

    def test_first_release_and_blocked_retry(self):
        # Attempts are not stored as successful state.
        self.assertTrue(should_build(self.release(), None))
        self.assertTrue(should_build(self.release(), None))

    def test_validated_release_is_unchanged(self):
        self.assertFalse(should_build(self.release(), {"release": self.release()}))

    def test_force_rebuild(self):
        self.assertTrue(should_build(self.release(), {"release": self.release()}, force=True))

    def test_new_release(self):
        self.assertTrue(should_build(self.release(), {"release": self.release("0.3.1")}))

    def test_no_downgrade_even_forced(self):
        with self.assertRaisesRegex(GateError, "downgrade"):
            should_build(self.release("0.3.1"), {"release": self.release()}, force=True)

    def test_moved_tag_even_forced(self):
        with self.assertRaisesRegex(GateError, "moved"):
            should_build(self.release(commit="b" * 40), {"release": self.release()}, force=True)

    @patch("qscopr.source.api")
    def test_latest_release_resolves_commit(self, api):
        api.side_effect = [
            {
                "draft": False,
                "prerelease": False,
                "tag_name": "v0.3.2",
                "html_url": "https://example.com/release",
                "published_at": "2026-10-08T09:38:03Z",
            },
            {"sha": "a" * 40},
        ]
        result = select_release("quickshell-mirror/quickshell")
        self.assertEqual(result["commit"], "a" * 40)
        self.assertIn("releases/latest", api.call_args_list[0].args[0])

    @patch("qscopr.source.api")
    def test_draft_or_prerelease_is_refused(self, api):
        for draft, prerelease in [(True, False), (False, True)]:
            api.return_value = {
                "draft": draft,
                "prerelease": prerelease,
            }
            with self.subTest(draft=draft), self.assertRaises(GateError):
                select_release("owner/repo")


class ArchiveTests(unittest.TestCase):
    def archive(self, destination, name, kind=tarfile.REGTYPE):
        path = destination / "archive.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            content = b"hello"
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            archive.addfile(member, io.BytesIO(content) if member.size else None)
        return path

    def test_extract_regular_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            archive = self.archive(path, "quickshell/CMakeLists.txt")
            source = extract_source(archive, path / "source")
            self.assertEqual((source / "CMakeLists.txt").read_text(), "hello")

    def test_reject_traversal_absolute_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name, kind in [
                ("root/../../escape", tarfile.REGTYPE),
                ("/tmp/escape", tarfile.REGTYPE),
                ("root/link", tarfile.SYMTYPE),
                ("root/device", tarfile.CHRTYPE),
            ]:
                with self.subTest(name=name), self.assertRaises(GateError):
                    extract_source(self.archive(path, name, kind), path / "source")
