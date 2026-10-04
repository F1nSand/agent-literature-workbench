import io
import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.audit_docker_image_layers import audit_saved_archive


class DockerImageLayerAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _archive(self, layers, tags=("workbench:test",)) -> Path:
        image_blobs: dict[str, bytes] = {}
        manifest = []
        for index, (tag, layer_files) in enumerate(zip(tags, layers, strict=True)):
            config_path = f"blobs/sha256/config-{index}"
            layer_content = self._layer(layer_files)
            layer_digest = hashlib.sha256(layer_content).hexdigest()
            layer_path = f"blobs/sha256/layer-{layer_digest}"
            image_blobs[config_path] = json.dumps({"config": {"Env": []}}).encode()
            image_blobs[layer_path] = layer_content
            manifest.append(
                {"Config": config_path, "RepoTags": [tag], "Layers": [layer_path]}
            )

        path = self.root / "images.tar"
        with tarfile.open(path, "w") as archive:
            self._add(archive, "manifest.json", json.dumps(manifest).encode())
            for name, content in image_blobs.items():
                self._add(archive, name, content)
        return path

    def _layer(self, files: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as layer:
            for name, content in files.items():
                self._add(layer, name, content)
        return output.getvalue()

    def _add(self, archive, name: str, content: bytes):
        entry = tarfile.TarInfo(name)
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))

    def test_clean_shared_layer_is_scanned_once_for_multiple_images(self):
        layer = {"app/main.py": b"def main(): return 'ready'"}
        path = self._archive((layer, layer), tags=("workbench:api", "workbench:worker"))

        result = audit_saved_archive(path)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["layers_scanned"], 1)
        self.assertEqual(result["images_scanned"], ["workbench:api", "workbench:worker"])

    def test_scans_oci_attestation_layers_as_well_as_runtime_layers(self):
        blobs = {}

        def descriptor(content: bytes, media_type: str):
            digest = hashlib.sha256(content).hexdigest()
            path = f"blobs/sha256/{digest}"
            blobs[path] = content
            return {"mediaType": media_type, "digest": f"sha256:{digest}", "size": len(content)}

        layer = descriptor(
            self._layer({"app/main.py": b"print('ready')"}),
            "application/vnd.oci.image.layer.v1.tar+gzip",
        )
        config = descriptor(b'{"config":{"Env":[]}}', "application/vnd.oci.image.config.v1+json")
        runtime_manifest = descriptor(
            json.dumps({"schemaVersion": 2, "config": config, "layers": [layer]}).encode(),
            "application/vnd.oci.image.manifest.v1+json",
        )
        attestation = descriptor(
            b'{"predicateType":"buildkit","statement":"OPENAI_API_KEY=' + b"x" * 32 + b'"}',
            "application/vnd.in-toto+json",
        )
        attestation_config = descriptor(b"{}", "application/vnd.oci.empty.v1+json")
        attestation_manifest = descriptor(
            json.dumps({"schemaVersion": 2, "config": attestation_config, "layers": [attestation]}).encode(),
            "application/vnd.oci.image.manifest.v1+json",
        )
        index = descriptor(
            json.dumps({"schemaVersion": 2, "manifests": [runtime_manifest, attestation_manifest]}).encode(),
            "application/vnd.oci.image.index.v1+json",
        )

        path = self.root / "oci-images.tar"
        with tarfile.open(path, "w") as archive:
            self._add(archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
            self._add(
                archive,
                "index.json",
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "manifests": [
                            {
                                **index,
                                "annotations": {"io.containerd.image.name": "workbench:oci"},
                            }
                        ],
                    }
                ).encode(),
            )
            for name, content in blobs.items():
                self._add(archive, name, content)

        result = audit_saved_archive(path)

        self.assertEqual(result["status"], "failed")
        self.assertIn("credential_assignment", {item["category"] for item in result["findings"]})
        self.assertEqual(result["layers_scanned"], 1)
        self.assertNotIn("x" * 32, json.dumps(result))

    def test_rejects_unknown_oci_layer_media_type_instead_of_skipping_it(self):
        blobs = {}

        def descriptor(content: bytes, media_type: str):
            digest = hashlib.sha256(content).hexdigest()
            path = f"blobs/sha256/{digest}"
            blobs[path] = content
            return {"mediaType": media_type, "digest": f"sha256:{digest}", "size": len(content)}

        config = descriptor(b"{}", "application/vnd.oci.image.config.v1+json")
        unknown_layer = descriptor(b"opaque-compressed-layer", "application/vnd.example.opaque")
        manifest = descriptor(
            json.dumps({"schemaVersion": 2, "config": config, "layers": [unknown_layer]}).encode(),
            "application/vnd.oci.image.manifest.v1+json",
        )
        path = self.root / "unsupported-oci-layer.tar"
        with tarfile.open(path, "w") as archive:
            self._add(
                archive,
                "index.json",
                json.dumps({"schemaVersion": 2, "manifests": [manifest]}).encode(),
            )
            for name, content in blobs.items():
                self._add(archive, name, content)

        with self.assertRaisesRegex(ValueError, "media type"):
            audit_saved_archive(path)

    def test_rejects_unknown_oci_descriptor_media_type_instead_of_scanning_raw_bytes(self):
        opaque = b"compressed or otherwise unsupported payload"
        digest = hashlib.sha256(opaque).hexdigest()
        path = self.root / "unsupported-oci-descriptor.tar"
        with tarfile.open(path, "w") as archive:
            self._add(
                archive,
                "index.json",
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "manifests": [
                            {
                                "mediaType": "application/vnd.example.opaque",
                                "digest": f"sha256:{digest}",
                                "size": len(opaque),
                            }
                        ],
                    }
                ).encode(),
            )
            self._add(archive, f"blobs/sha256/{digest}", opaque)

        with self.assertRaisesRegex(ValueError, "media type"):
            audit_saved_archive(path)

    def test_finds_secret_in_old_layer_even_when_later_layer_whiteouts_file(self):
        value = "SILICONFLOW_API_KEY=" + "x" * 32
        path = self._archive(
            (
                {"app/settings.py": value.encode()},
                {"app/.wh.settings.py": b""},
            ),
            tags=("workbench:api", "workbench:api"),
        )

        result = audit_saved_archive(path)

        self.assertEqual(result["status"], "failed")
        self.assertIn("credential_assignment", {item["category"] for item in result["findings"]})
        self.assertNotIn("x" * 32, json.dumps(result))
        self.assertIn("app/settings.py", json.dumps(result))

    def test_ignores_python_identifier_reference_but_keeps_quoted_credentials(self):
        code = b"def run(previously_issued_authorization_reference):\n    token = previously_issued_authorization_reference\n"
        path = self._archive(({"app/module.py": code},))

        result = audit_saved_archive(path)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["findings"], [])

        quoted_path = self._archive(
            ({"app/module.py": b'token = "abcdefghijklmnopqrstuvwxyz123456"\n'},)
        )
        quoted_result = audit_saved_archive(quoted_path)
        self.assertEqual(quoted_result["status"], "failed")
        self.assertIn("credential_assignment", {item["category"] for item in quoted_result["findings"]})

    def test_ignores_only_the_common_local_postgres_documentation_example(self):
        safe_path = self._archive(
            ({"app/example.py": b'"""postgres://postgres:postgres@localhost:5432/postgres"""'},)
        )
        self.assertEqual(audit_saved_archive(safe_path)["status"], "passed")

        unsafe_path = self._archive(
            ({"app/config.py": b'DATABASE_URL = "postgres://workbench:long-example-password@db:5432/workbench"'},)
        )
        unsafe_result = audit_saved_archive(unsafe_path)
        self.assertEqual(unsafe_result["status"], "failed")
        self.assertIn("database_url_password", {item["category"] for item in unsafe_result["findings"]})

    def test_scans_personal_paths_in_binary_file_bytes(self):
        separator = chr(92)
        local_path = "C:" + separator + "Users" + separator + "Researcher" + separator + "paper.pdf"
        path = self._archive(({"app/module.pyc": b"\x00" + local_path.encode() + b"\x00"},))

        result = audit_saved_archive(path)

        self.assertEqual(result["status"], "failed")
        self.assertIn("personal_absolute_path", {item["category"] for item in result["findings"]})
        self.assertNotIn(local_path, json.dumps(result))

    def test_reviewed_upstream_path_requires_exact_file_hash(self):
        separator = chr(92)
        local_path = "C:" + separator + "Users" + separator + "ExampleUser" + separator + "paper.pdf"
        content = b"\x00" + local_path.encode() + b"\x00"
        allowlist = self.root / "allowlist.json"
        allowlist.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "reviewed_upstream_findings": [
                        {
                            "category": "personal_absolute_path",
                            "path": "app/upstream.bin",
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "basis": "Reviewed upstream fixture.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        path = self._archive(({"app/upstream.bin": content},))

        with patch("scripts.audit_docker_image_layers.REVIEWED_PATH_ALLOWLIST", allowlist):
            result = audit_saved_archive(path)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["reviewed_findings"]), 1)
        self.assertEqual(result["findings"], [])
        self.assertNotIn(local_path, json.dumps(result))

        changed_path = self._archive(({"app/upstream.bin": content + b"changed"},))
        with patch("scripts.audit_docker_image_layers.REVIEWED_PATH_ALLOWLIST", allowlist):
            changed_result = audit_saved_archive(changed_path)
        self.assertEqual(changed_result["status"], "failed")

    def test_rejects_env_pdf_and_private_key_content(self):
        path = self._archive(
            (
                {
                    "app/.env": b"not-read-or-printed",
                    "app/paper.pdf": b"pdf bytes",
                    "app/identity.txt": b"-----BEGIN PRIVATE KEY-----",
                },
            )
        )

        result = audit_saved_archive(path)

        self.assertEqual(result["status"], "failed")
        categories = {item["category"] for item in result["findings"]}
        self.assertTrue({"credential_file", "private_or_opaque_archive", "private_key_block"}.issubset(categories))
        self.assertNotIn("not-read-or-printed", json.dumps(result))

    def test_rejects_unsafe_manifest_layer_path(self):
        path = self.root / "unsafe.tar"
        manifest = [{"Config": "config.json", "RepoTags": ["workbench:test"], "Layers": ["../../secret.tar"]}]
        with tarfile.open(path, "w") as archive:
            self._add(archive, "manifest.json", json.dumps(manifest).encode())
            self._add(archive, "config.json", b"{}")

        with self.assertRaisesRegex(ValueError, "archive path"):
            audit_saved_archive(path)

    def test_corrupted_nested_layer_fails_closed(self):
        path = self.root / "corrupt.tar"
        manifest = [{"Config": "config.json", "RepoTags": ["workbench:test"], "Layers": ["layer.tar"]}]
        with tarfile.open(path, "w") as archive:
            self._add(archive, "manifest.json", json.dumps(manifest).encode())
            self._add(archive, "config.json", b"{}")
            self._add(archive, "layer.tar", b"not a tar stream")

        with self.assertRaises(ValueError):
            audit_saved_archive(path)

    def test_oversized_text_member_fails_closed(self):
        path = self._archive(({"app/readme.txt": b"12345"},))
        with patch("scripts.audit_docker_image_layers.MAX_TEXT_FILE_BYTES", 4):
            result = audit_saved_archive(path)

        self.assertEqual(result["status"], "failed")
        self.assertIn("oversized_text_file", {item["category"] for item in result["findings"]})

    def test_report_contains_no_archive_or_report_absolute_path_and_never_overwrites(self):
        archive = self._archive(({"app/main.py": b"safe"},))
        report = self.root / "audit.json"

        audit_saved_archive(archive, report)

        payload = report.read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), payload)
        self.assertEqual(json.loads(payload)["status"], "passed")
        with self.assertRaisesRegex(ValueError, "already exists"):
            audit_saved_archive(archive, report)


if __name__ == "__main__":
    unittest.main()
