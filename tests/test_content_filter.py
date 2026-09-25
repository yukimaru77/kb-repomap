import unittest
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

from kb_repo import repomix_candidates, skip_reason, tracked_files


class ContentFilterTest(unittest.TestCase):
    def test_repomix_mechanical_selection_is_applied_to_tracked_files(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for name, body in {
                "app.py": "print('useful')\n",
                "dist/generated.js": "const generated = true;\n",
                "noise.txt": "unneeded\n",
                ".repomixignore": "noise.txt\n",
            }.items():
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            subprocess.run(["git", "-C", str(repo), "add", "-f", "."], check=True)
            (repo / "untracked.py").write_text("print('untracked')\n")

            selected = repomix_candidates(repo) & set(tracked_files(repo))

        self.assertIn(PurePosixPath("app.py"), selected)
        self.assertNotIn(PurePosixPath("dist/generated.js"), selected)
        self.assertNotIn(PurePosixPath("noise.txt"), selected)
        self.assertNotIn(PurePosixPath("untracked.py"), selected)

    def reason(self, path, size):
        return skip_reason(PurePosixPath(path), b"x" * size, 600_000)

    def test_excludes_lottie_json(self):
        self.assertEqual(
            self.reason("web/assets/json/lottie/animation.json", 1_000),
            "data-driven visual asset",
        )

    def test_excludes_large_recorded_test_data(self):
        self.assertEqual(
            self.reason("spec/cassettes/payment/success.yml", 100_000),
            "large test fixture/cassette/snapshot",
        )
        self.assertEqual(
            self.reason("db/fixtures/products.csv", 150_000),
            "large test fixture/cassette/snapshot",
        )

    def test_excludes_large_static_web_assets(self):
        self.assertEqual(
            self.reason("app/assets/javascripts/vendor-library.js", 150_000),
            "large static/generated web asset",
        )
        self.assertEqual(
            self.reason("static/developers/openapi.html", 150_000),
            "large static/generated web asset",
        )

    def test_excludes_generated_api_bundle(self):
        self.assertEqual(
            self.reason("openapi/bundled.yaml", 10_000),
            "generated/bundled API description",
        )

    def test_excludes_large_structured_data(self):
        self.assertEqual(
            self.reason("config/settings/prices.json", 200_000),
            "large structured data/snapshot",
        )

    def test_keeps_small_configuration_and_handwritten_api_source(self):
        self.assertIsNone(self.reason("config/settings/prices.json", 20_000))
        self.assertIsNone(self.reason("openapi/source.yaml", 50_000))

    def test_keeps_large_source_and_design_documents(self):
        self.assertIsNone(self.reason("app/services/large_service.rb", 300_000))
        self.assertIsNone(self.reason("docs/design/architecture.md", 300_000))


if __name__ == "__main__":
    unittest.main()
