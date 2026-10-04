import io
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.release_audit import audit_bundle, main


class ReleaseAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "release"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative_path: str, content: str = "safe") -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_clean_text_bundle_with_env_example_passes(self):
        self.write("README.md", "Instructions for local setup.")
        self.write(".env.example", "SILICONFLOW_API_KEY=replace-with-your-key")
        self.write("migrations/script.py.mako", "# migration template")
        self.write("migrations/versions/.gitkeep", "")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["text_files_scanned"], 4)

    def test_detects_provider_key_and_personal_absolute_path_without_echoing_values(self):
        self.write("config.txt", "provider=sk-" + "a" * 28 + "\n")
        separator = chr(92)
        private_path = "C:" + separator + "Users" + separator + "Researcher" + separator + "Desktop" + separator + "paper.pdf"
        self.write("notes.md", "Local file: " + private_path)

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            {item["category"] for item in result["findings"]},
            {"provider_key", "personal_absolute_path"},
        )
        serialized = str(result)
        self.assertNotIn("a" * 28, serialized)

    def test_detects_common_windows_path_encodings_without_flagging_template(self):
        separator = chr(92)
        windows_path = "C:" + separator + "Users" + separator + "Researcher" + separator + "Desktop" + separator + "paper.pdf"
        escaped_path = "C:" + separator * 2 + "Users" + separator * 2 + "Researcher" + separator * 2 + "Desktop" + separator * 2 + "paper.pdf"
        file_url = "file:///" + "C:" + "/" + "Users" + "/" + "Researcher" + "/" + "Desktop" + "/" + "paper.pdf"
        paths = [windows_path, escaped_path, file_url]
        for index, path in enumerate(paths):
            self.write(f"path-{index}.md", path)
        template_path = "C:" + separator + "Users" + separator + "<username>" + separator + "Desktop" + separator + "paper.pdf"
        self.write("template.md", template_path)

        result = audit_bundle(self.root)

        flagged_paths = {
            item["path"]
            for item in result["findings"]
            if item["category"] == "personal_absolute_path"
        }
        self.assertEqual(flagged_paths, {"path-0.md", "path-1.md", "path-2.md"})

    def test_detects_literal_secret_assigned_to_named_config(self):
        self.write("config.txt", "SILICONFLOW_API_KEY=" + "b" * 32)

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["findings"][0]["category"], "credential_assignment")

    def test_rejects_env_and_paper_or_backup_archives(self):
        self.write(".env", "SILICONFLOW_API_KEY=not-a-real-key")
        self.write("production.env", "unclassified-content")
        (self.root / "paper.pdf").write_bytes(b"pdf")
        (self.root / "database.dump").write_bytes(b"dump")
        (self.root / "identity.pem").write_bytes(b"private key material")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            {item["category"] for item in result["findings"]},
            {"credential_file", "private_or_opaque_archive"},
        )

    def test_media_requires_manual_review_instead_of_passing(self):
        (self.root / "screenshot.png").write_bytes(b"opaque")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "needs_manual_review")
        self.assertEqual(result["manual_review_files"], ["screenshot.png"])

    def _write_media_review(self, relative_path: str, reviewed_sha256: str | None = None):
        payload = (self.root / relative_path).read_bytes()
        manifest = {
            "schema_version": 1,
            "reviewer": "Codex agent",
            "reviewed_at_utc": "2026-09-30T10:50:00Z",
            "review_method": "Browser rendering and source inspection",
            "files": [
                {
                    "path": relative_path,
                    "sha256": reviewed_sha256 or hashlib.sha256(payload).hexdigest(),
                }
            ],
        }
        (self.root / "RELEASE-MANUAL-REVIEW.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def test_matching_media_review_manifest_resolves_only_exact_asset_hash(self):
        self.write("diagram.svg", "<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        self._write_media_review("diagram.svg")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["manual_review_files"], [])
        self.assertEqual(
            result["reviewed_assets"],
            [
                {
                    "path": "diagram.svg",
                    "sha256": hashlib.sha256(
                        (self.root / "diagram.svg").read_bytes()
                    ).hexdigest(),
                    "reviewer": "Codex agent",
                    "reviewed_at_utc": "2026-09-30T10:50:00Z",
                    "review_method": "Browser rendering and source inspection",
                }
            ],
        )
        self.assertIsNotNone(result["manual_review_manifest_sha256"])

    def test_media_hash_change_invalidates_review_and_fails_audit(self):
        asset = self.write("diagram.svg", "<svg></svg>")
        self._write_media_review("diagram.svg")
        asset.write_text("<svg><script>alert(1)</script></svg>", encoding="utf-8")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["manual_review_files"], ["diagram.svg"])
        self.assertIn(
            "manual_review_hash_mismatch",
            {item["category"] for item in result["findings"]},
        )

    def test_media_review_cannot_suppress_text_security_findings(self):
        self.write("diagram.svg", "<svg>sk-" + "d" * 28 + "</svg>")
        self._write_media_review("diagram.svg")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertIn("provider_key", {item["category"] for item in result["findings"]})

    def test_media_review_manifest_rejects_non_media_targets(self):
        self.write("README.md", "safe")
        self._write_media_review("README.md")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "manual_review_unreviewable_target",
            {item["category"] for item in result["findings"]},
        )

    def test_media_review_manifest_rejects_unknown_fields(self):
        self.write("diagram.svg", "<svg></svg>")
        self._write_media_review("diagram.svg")
        manifest_path = self.root / "RELEASE-MANUAL-REVIEW.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["reviewed_automatically"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "manual_review_manifest_invalid",
            {item["category"] for item in result["findings"]},
        )

    def test_media_review_manifest_rejects_path_traversal(self):
        self.write("diagram.svg", "<svg></svg>")
        self._write_media_review("diagram.svg")
        manifest_path = self.root / "RELEASE-MANUAL-REVIEW.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../diagram.svg"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "manual_review_manifest_invalid",
            {item["category"] for item in result["findings"]},
        )

    def test_media_review_refuses_oversized_assets(self):
        self.write("diagram.svg", "<svg></svg>")
        self._write_media_review("diagram.svg")
        with patch("scripts.release_audit.MAX_MANUAL_REVIEW_ASSET_BYTES", 2):
            result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "manual_review_asset_too_large",
            {item["category"] for item in result["findings"]},
        )

    def test_svg_is_text_scanned_and_still_requires_visual_review(self):
        self.write("diagram.svg", "<svg>sk-" + "c" * 28 + "</svg>")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["manual_review_files"], ["diagram.svg"])
        self.assertIn("provider_key", {item["category"] for item in result["findings"]})

    def test_report_must_be_outside_staged_release(self):
        report = self.root / "audit.json"

        with self.assertRaisesRegex(ValueError, "outside"):
            audit_bundle(self.root, report)

    def test_report_does_not_overwrite_an_existing_file(self):
        report = Path(self.temp.name) / "audit.json"
        report.write_text("preserve", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "already exists"):
            audit_bundle(self.root, report)
        self.assertEqual(report.read_text(encoding="utf-8"), "preserve")

    def test_cli_reports_findings_without_echoing_secret_or_absolute_root(self):
        dummy_secret = "z" * 32
        self.write("config.txt", "SILICONFLOW_API_KEY=" + dummy_secret)
        output = io.StringIO()
        with patch("sys.argv", ["release_audit.py", str(self.root)]), patch(
            "sys.stdout", output
        ):
            exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertIn("credential_assignment", output.getvalue())
        self.assertIn("config.txt", output.getvalue())
        self.assertNotIn(dummy_secret, output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_cli_error_does_not_echo_absolute_directory(self):
        missing_root = Path(self.temp.name) / "does-not-exist"
        errors = io.StringIO()
        with patch("sys.argv", ["release_audit.py", str(missing_root)]), patch(
            "sys.stderr", errors
        ):
            exit_code = main()

        self.assertEqual(exit_code, 2)
        self.assertIn("check the staged directory", errors.getvalue())
        self.assertNotIn(str(missing_root), errors.getvalue())

    def test_empty_release_directory_does_not_pass(self):
        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["findings"][0]["category"], "empty_release_bundle")

    def test_development_directories_are_rejected_without_scanning_them(self):
        private = self.root / ".playwright-mcp"
        private.mkdir()
        (private / "capture.txt").write_text("private browser capture", encoding="utf-8")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["findings"],
            [{"category": "release_development_directory", "path": ".playwright-mcp"}],
        )
        self.assertEqual(result["text_files_scanned"], 0)

    def test_oversized_text_file_fails_closed(self):
        self.write("large-report.txt", "safe")
        with patch("scripts.release_audit.MAX_TEXT_FILE_BYTES", 2):
            result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["findings"][0]["category"], "oversized_text_file")

    def test_symlink_is_rejected_without_following_target(self):
        target = Path(self.temp.name) / "outside.txt"
        target.write_text("safe", encoding="utf-8")
        link = self.root / "external.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")

        result = audit_bundle(self.root)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["findings"], [{"category": "symlink", "path": "external.txt"}])


if __name__ == "__main__":
    unittest.main()
