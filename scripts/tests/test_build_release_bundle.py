import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_release_bundle import build_release_bundle


class BuildReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "project"
        self.source.mkdir()
        self._create_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, relative_path: str, content: str = "safe") -> Path:
        path = self.source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _create_fixture(self):
        for name in (
            ".dockerignore",
            ".env.example",
            "README.md",
            "compose.yaml",
            "compose.production.yaml",
            "本地配置说明.md",
        ):
            self._write(name)
        for directory in (
            "backend",
            "frontend",
            "config",
            "scripts",
            "docs/design",
            "docs/demo",
            "docs/operations",
        ):
            (self.source / directory).mkdir(parents=True, exist_ok=True)
        for relative in (
            "backend/Dockerfile",
            "backend/Dockerfile.runtime",
            "backend/Dockerfile.runtime.dockerignore",
            "backend/Dockerfile.converter",
            "backend/README.md",
            "backend/requirements.lock",
            "backend/requirements.runtime.lock",
            "backend/pyproject.toml",
            "backend/alembic.ini",
            "backend/app/main.py",
            "backend/converter/server.py",
            "backend/tests/test_private.py",
            "backend/migrations/env.py",
            "frontend/Dockerfile",
            "frontend/Dockerfile.runtime",
            "frontend/Dockerfile.runtime.dockerignore",
            "frontend/nginx.production.conf",
            "frontend/package.json",
            "frontend/pnpm-workspace.yaml",
            "frontend/pdfAssets.ts",
            "frontend/pnpm-lock.yaml",
            "frontend/src/main.ts",
            "config/model-profiles.yaml",
            "scripts/backup.ps1",
            "scripts/audit_docker_image_layers.py",
            "scripts/docker_image_layer_audit_allowlist.json",
            "scripts/compose.ps1",
            "scripts/compose.restore-test.yaml",
            "scripts/restore_test.ps1",
            "scripts/verify_restored_library.py",
            "scripts/release_audit.py",
            "scripts/build_release_bundle.py",
            "docs/design/architecture.md",
            "docs/demo/runbook.md",
            "docs/operations/data.md",
        ):
            self._write(relative)

    def test_copies_runtime_allowlist_and_excludes_local_or_development_material(self):
        (self.source / ".env").write_text("private", encoding="utf-8")
        self._write("backend/.env.production", "SILICONFLOW_API_KEY=private")
        self._write("示例论文文档与问题集/paper.pdf", "paper")
        self._write("evals/questions.md", "private evaluation content")
        self._write("work/plans/internal.md", "development plan")
        self._write("scripts/m0_siliconflow_probe.py", "development helper")
        self._write("scripts/tests/test_private.py", "development test")
        self._write("frontend/node_modules/cache.js", "generated dependency")
        self._write("frontend/dist/app.js", "generated build")
        self._write("backend/.pytest_cache/state", "generated test state")

        output = self.base / "release"
        result = build_release_bundle(self.source, output)

        self.assertTrue(result["published"])
        self.assertEqual(result["audit"]["status"], "passed")
        self.assertTrue((output / "backend/Dockerfile").is_file())
        self.assertTrue((output / "backend/Dockerfile.converter").is_file())
        self.assertTrue((output / "backend/converter/server.py").is_file())
        self.assertTrue((output / "frontend/pnpm-lock.yaml").is_file())
        self.assertTrue((output / "frontend/pnpm-workspace.yaml").is_file())
        self.assertTrue((output / "frontend/pdfAssets.ts").is_file())
        self.assertTrue((output / "本地配置说明.md").is_file())
        for excluded in (
            ".env",
            "backend/.env.production",
            "backend/tests",
            "示例论文文档与问题集",
            "evals",
            "work",
            "scripts/m0_siliconflow_probe.py",
            "scripts/tests",
            "frontend/node_modules",
            "frontend/dist",
            "backend/.pytest_cache",
        ):
            self.assertFalse((output / excluded).exists(), excluded)

    def test_manifest_uses_only_relative_paths_and_matches_file_hashes(self):
        output = self.base / "release"

        build_release_bundle(self.source, output)

        manifest = json.loads((output / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertNotIn(str(self.source), json.dumps(manifest))
        entries = {entry["path"]: entry for entry in manifest["files"]}
        self.assertIn("backend/Dockerfile", entries)
        payload = (output / "backend/Dockerfile").read_bytes()
        self.assertEqual(entries["backend/Dockerfile"]["size_bytes"], len(payload))
        self.assertEqual(entries["backend/Dockerfile"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertNotIn("RELEASE-MANIFEST.json", entries)

    def test_manifest_is_deterministic_for_same_source(self):
        first = self.base / "release-one"
        second = self.base / "release-two"

        build_release_bundle(self.source, first)
        build_release_bundle(self.source, second)

        self.assertEqual(
            (first / "RELEASE-MANIFEST.json").read_bytes(),
            (second / "RELEASE-MANIFEST.json").read_bytes(),
        )

    def test_refuses_to_overwrite_existing_output(self):
        output = self.base / "release"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "already exists"):
            build_release_bundle(self.source, output)

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_refuses_output_inside_selected_source_tree(self):
        output = self.source / "backend" / "release"

        with self.assertRaisesRegex(ValueError, "source"):
            build_release_bundle(self.source, output)

        self.assertFalse(output.exists())

    def test_refuses_audit_report_inside_final_output(self):
        output = self.base / "release"

        with self.assertRaisesRegex(ValueError, "outside the output"):
            build_release_bundle(self.source, output, output / "audit.json")

        self.assertFalse(output.exists())

    def test_refuses_audit_report_inside_packaged_source(self):
        output = self.base / "release"

        with self.assertRaisesRegex(ValueError, "packaged source"):
            build_release_bundle(self.source, output, self.source / "backend" / "audit.json")

        self.assertFalse(output.exists())
        self.assertFalse((self.source / "backend" / "audit.json").exists())

    def test_failed_audit_does_not_publish_bundle(self):
        self._write("backend/leaked.txt", "SILICONFLOW_API_KEY=" + "x" * 32)
        output = self.base / "release"

        result = build_release_bundle(self.source, output)

        self.assertFalse(result["published"])
        self.assertEqual(result["audit"]["status"], "failed")
        self.assertFalse(output.exists())

    def test_manual_review_assets_are_staged_but_never_reported_as_passed(self):
        self._write("frontend/public/logo.svg", "<svg></svg>")
        output = self.base / "release"

        result = build_release_bundle(self.source, output)

        self.assertTrue(result["published"])
        self.assertEqual(result["audit"]["status"], "needs_manual_review")
        self.assertEqual(result["audit"]["manual_review_files"], ["frontend/public/logo.svg"])

    def test_explicit_media_review_manifest_is_packaged_and_hash_verified(self):
        import hashlib

        asset = self._write("frontend/public/logo.svg", "<svg></svg>")
        review_manifest = self.base / "media-review.json"
        review_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "reviewer": "Codex agent",
                    "reviewed_at_utc": "2026-09-30T10:50:00Z",
                    "review_method": "Browser visual inspection",
                    "files": [
                        {
                            "path": "frontend/public/logo.svg",
                            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        output = self.base / "release"

        result = build_release_bundle(
            self.source,
            output,
            manual_review_manifest_path=review_manifest,
        )

        self.assertTrue(result["published"])
        self.assertEqual(result["audit"]["status"], "passed")
        embedded = output / "RELEASE-MANUAL-REVIEW.json"
        self.assertTrue(embedded.is_file())
        manifest = json.loads((output / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
        entry = next(
            item for item in manifest["files"] if item["path"] == "RELEASE-MANUAL-REVIEW.json"
        )
        self.assertEqual(entry["sha256"], hashlib.sha256(embedded.read_bytes()).hexdigest())

    def test_stale_media_review_manifest_prevents_bundle_publication(self):
        self._write("frontend/public/logo.svg", "<svg></svg>")
        review_manifest = self.base / "media-review.json"
        review_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "reviewer": "Codex agent",
                    "reviewed_at_utc": "2026-09-30T10:50:00Z",
                    "review_method": "Browser visual inspection",
                    "files": [
                        {"path": "frontend/public/logo.svg", "sha256": "0" * 64}
                    ],
                }
            ),
            encoding="utf-8",
        )
        output = self.base / "release"

        result = build_release_bundle(
            self.source,
            output,
            manual_review_manifest_path=review_manifest,
        )

        self.assertFalse(result["published"])
        self.assertEqual(result["audit"]["status"], "failed")
        self.assertFalse(output.exists())

    def test_refuses_symbolic_links_in_selected_source_tree(self):
        target = self.source / "backend" / "app" / "main.py"
        link = self.source / "backend" / "app" / "alias.py"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")

        with self.assertRaisesRegex(ValueError, "link"):
            build_release_bundle(self.source, self.base / "release")


if __name__ == "__main__":
    unittest.main()
