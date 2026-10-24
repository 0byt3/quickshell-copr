import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from qscopr.publish import matching_build, publish, publish_pipeline, validate_manifest


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        (self.output / "test.src.rpm").write_bytes(b"test")
        self.manifest = {
            "project": "owner/quickshell",
            "chroot": "fedora-44-x86_64",
            "srpm": "test.src.rpm",
            "sha256": hashlib.sha256(b"test").hexdigest(),
            "version": "0.3.2",
            "rpm_release": 1,
        }
        self.client = Mock()
        self.client.project_proxy.get.return_value = {
            "chroot_repos": {
                "fedora-44-x86_64": "https://example.com/repo",
            },
        }
        self.client.project_chroot_proxy.get.return_value = {}
        self.client.build_proxy.get_list.return_value = []
        self.client.build_proxy.create_from_file.return_value = {"id": 42}
        self.client.build_proxy.get.return_value = {"state": "succeeded"}
        self.client.build_chroot_proxy.get.return_value = {"state": "succeeded"}

    def build(self, state="succeeded", version="0.3.2-1.fc44"):
        return {
            "id": 42,
            "state": state,
            "source_package": {
                "name": "quickshell",
                "version": version,
            },
            "chroots": ["fedora-44-x86_64"],
        }

    def test_matching_nvr(self):
        self.assertTrue(matching_build(self.build(), self.manifest))
        self.assertTrue(matching_build(self.build(version="0.3.2-1"), self.manifest))
        self.assertFalse(matching_build(self.build(version="0.3.2-2.fc44"), self.manifest))

    def test_wrong_chroot_does_not_match(self):
        build = self.build()
        build["chroots"] = ["fedora-45-x86_64"]
        self.assertFalse(matching_build(build, self.manifest))

    def test_upload_and_wait(self):
        result = publish(self.client, self.manifest, self.output)
        self.assertEqual(result["state"], "succeeded")
        options = self.client.build_proxy.create_from_file.call_args.kwargs["buildopts"]
        self.assertFalse(options["enable_net"])
        self.assertEqual(options["chroots"], ["fedora-44-x86_64"])

    def test_duplicate_is_not_uploaded(self):
        self.client.build_proxy.get_list.return_value = [self.build()]
        result = publish(self.client, self.manifest, self.output)
        self.assertTrue(result["already_submitted"])
        self.client.build_proxy.create_from_file.assert_not_called()

    def test_chroot_must_succeed(self):
        self.client.build_chroot_proxy.get.return_value = {"state": "skipped"}
        with self.assertRaises(RuntimeError):
            publish(self.client, self.manifest, self.output)

    def test_failed_duplicate_requires_new_release(self):
        self.client.build_proxy.get_list.return_value = [self.build(state="failed")]
        with self.assertRaisesRegex(ValueError, "higher RPM Release"):
            publish(self.client, self.manifest, self.output)
        self.client.build_proxy.create_from_file.assert_not_called()

    def test_history_is_paginated(self):
        self.client.build_proxy.get_list.side_effect = [
            [self.build(version="0.3.1-1")],
            [self.build()],
        ]
        publish(self.client, self.manifest, self.output)
        self.assertEqual(self.client.build_proxy.get_list.call_count, 2)
        self.client.build_proxy.create_from_file.assert_not_called()

    def test_extra_repositories_refused(self):
        self.client.project_chroot_proxy.get.return_value = {"additional_repos": ["copr://other/project"]}
        with self.assertRaisesRegex(ValueError, "extra repositories"):
            publish(self.client, self.manifest, self.output)
        self.client.build_proxy.create_from_file.assert_not_called()

    def test_checksum_and_traversal_refused(self):
        for key, value in [("sha256", "0" * 64), ("srpm", "../test.src.rpm")]:
            manifest = self.manifest | {key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_manifest(manifest, self.output)

    def test_pending_source_build_blocks_new_upload(self):
        self.client.build_proxy.get_list.return_value = [{
            "id": 41,
            "state": "pending",
            "source_package": None,
        }]
        with self.assertRaisesRegex(ValueError, "pending"):
            publish(self.client, self.manifest, self.output)
        self.client.build_proxy.create_from_file.assert_not_called()

    @patch("qscopr.publish.time.sleep")
    def test_monitor_failure_is_recorded(self, sleep):
        self.client.build_proxy.get.return_value = {"state": "failed"}
        with self.assertRaises(RuntimeError):
            publish(self.client, self.manifest, self.output)
        result = json.loads((self.output / "copr-result.json").read_text())
        self.assertEqual(result["state"], "failed")

    def dependency(self):
        return self.manifest | {
            "name": "cpptrace",
            "version": "1.0.4",
        }

    @patch("qscopr.publish.publish")
    def test_cpptrace_is_published_before_quickshell(self, upload):
        dependency = self.dependency()
        manifest = self.manifest | {"prerequisites": [dependency]}
        upload.side_effect = [{"build_id": 41}, {"build_id": 42}]
        publish_pipeline(self.client, manifest, self.output)
        self.assertEqual(upload.call_args_list[0].args[1]["name"], "cpptrace")
        self.assertEqual(upload.call_args_list[1].args[1]["prerequisite_build_id"], 41)

    def test_completed_prerequisite_is_audited_without_invalid_batch_option(self):
        result = publish(
            self.client,
            self.manifest | {"prerequisite_build_id": 41},
            self.output,
        )
        options = self.client.build_proxy.create_from_file.call_args.kwargs["buildopts"]
        self.assertNotIn("after_build_id", options)
        self.assertEqual(result["prerequisite_build_id"], 41)
        saved = json.loads((self.output / "copr-result.json").read_text())
        self.assertEqual(saved["prerequisite_build_id"], 41)

    @patch("qscopr.publish.publish", side_effect=RuntimeError("cpptrace failed"))
    def test_cpptrace_failure_prevents_quickshell_upload(self, upload):
        manifest = self.manifest | {"prerequisites": [self.dependency()]}
        with self.assertRaises(RuntimeError):
            publish_pipeline(self.client, manifest, self.output)
        self.assertEqual(upload.call_count, 1)

    @patch("qscopr.publish.publish")
    def test_both_checksums_are_checked_before_any_upload(self, upload):
        manifest = self.manifest | {"prerequisites": [self.dependency() | {"sha256": "0" * 64}]}
        with self.assertRaises(ValueError):
            publish_pipeline(self.client, manifest, self.output)
        upload.assert_not_called()

    def test_cpptrace_duplicate_matches_its_own_name(self):
        build = self.build(version="1.0.4-1.fc44")
        build["source_package"]["name"] = "cpptrace"
        self.assertTrue(matching_build(build, self.dependency()))
        self.assertFalse(matching_build(build, self.manifest))
