import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import kb_api
import kb_decrypt
import kb_repo_url


class RRConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "rr.json"
        self.key = self.root / "keys" / "client.key"
        self.key.parent.mkdir()
        self.key.write_text("test-key\n")

    def write_config(self, **settings):
        self.config.write_text(json.dumps({"base_url": "https://first.invalid/v1", "key_file": "keys/client.key",
                                           **settings}))
        return {"KB_RR_CONFIG": str(self.config)}

    def write_legacy_config(self, path=None, **settings):
        path = path or self.root / "bridge.json"
        path.write_text(json.dumps({"origin": "https://legacy.invalid", "key_file": "keys/client.key", **settings}))
        return path

    def test_base_url_is_used_verbatim_and_changes_are_read_each_time(self):
        environ = self.write_config()
        self.assertEqual(kb_api.rr_configuration(environ), ("https://first.invalid/v1", "test-key"))
        self.write_config(base_url="https://changed.invalid/some/rr/")
        self.assertEqual(kb_api.rr_configuration(environ), ("https://changed.invalid/some/rr", "test-key"))
        self.assertEqual(environ, {"KB_RR_CONFIG": str(self.config)})

    def test_environment_fields(self):
        self.assertEqual(kb_api.rr_configuration({"KB_RR_BASE_URL": "http://127.0.0.1:18473/_pool/rr",
                                                  "KB_RR_KEY_FILE": str(self.key)}),
                         ("http://127.0.0.1:18473/_pool/rr", "test-key"))

    def test_deprecated_pool_names_still_work(self):
        legacy = self.write_legacy_config()
        self.assertEqual(kb_api.rr_configuration({"KB_POOL_CONFIG": str(legacy)}),
                         ("https://legacy.invalid/_pool/rr", "test-key"))
        self.assertEqual(kb_api.rr_configuration({"KB_POOL_ORIGIN": "http://localhost:8000/",
                                                  "KB_POOL_KEY_FILE": str(self.key)}),
                         ("http://localhost:8000/_pool/rr", "test-key"))
        # The new JSON variable also accepts the old JSON shape.
        self.assertEqual(kb_api.rr_configuration({"KB_RR_CONFIG": str(legacy)})[0], "https://legacy.invalid/_pool/rr")

    def test_new_names_win_over_old(self):
        legacy = self.write_legacy_config()
        other = self.root / "other.key"
        other.write_text("other-key")
        old = {"KB_POOL_CONFIG": str(legacy), "KB_POOL_ORIGIN": "https://old-field.invalid",
               "KB_POOL_KEY_FILE": str(other)}
        # New JSON beats both old JSON and old fields.
        self.assertEqual(kb_api.rr_configuration({**old, **self.write_config()}),
                         ("https://first.invalid/v1", "test-key"))
        # New fields beat everything.
        self.assertEqual(kb_api.rr_configuration({**old, **self.write_config(), "KB_RR_BASE_URL": "https://new.invalid/x",
                                                  "KB_RR_KEY_FILE": str(other)}),
                         ("https://new.invalid/x", "other-key"))
        # Old fields still beat old JSON.
        self.assertEqual(kb_api.rr_configuration(old), ("https://old-field.invalid/_pool/rr", "other-key"))

    def test_state_directory_fallback_and_home_expansion_for_legacy_json(self):
        for settings, key_path in (({}, self.root / "state" / "client.key"),
                                   ({"state_dir": "alternate"}, self.root / "alternate" / "client.key"),
                                   ({"state_dir": "~/pool-state"}, self.root / "pool-state" / "client.key"),
                                   ({"key_file": "~/home-key"}, self.root / "home-key")):
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text("fallback-key")
            (self.root / "bridge.json").write_text(json.dumps({"origin": "http://localhost:8000", **settings}))
            with self.subTest(settings=settings), mock.patch.dict(os.environ, {"HOME": str(self.root)}):
                self.assertEqual(kb_api.rr_configuration({"KB_POOL_CONFIG": "~/bridge.json"}),
                                 ("http://localhost:8000/_pool/rr", "fallback-key"))

    def test_explicit_environment_overrides_json_including_private_http_false(self):
        environ = self.write_config(base_url="http://private.invalid:18473/rr", private_http=True)
        self.assertEqual(kb_api.rr_configuration(environ)[0], "http://private.invalid:18473/rr")
        with self.assertRaisesRegex(ValueError, "remote HTTP"):
            kb_api.rr_configuration({**environ, "KB_RR_PRIVATE_HTTP": "0"})
        override_key = self.root / "override.key"
        override_key.write_text("override-key")
        self.assertEqual(kb_api.rr_configuration({**environ, "KB_RR_BASE_URL": "https://explicit.invalid",
                         "KB_RR_KEY_FILE": str(override_key), "KB_RR_PRIVATE_HTTP": "0"}),
                         ("https://explicit.invalid", "override-key"))

    def test_missing_explicit_json_is_an_error_even_when_fields_are_overridden(self):
        for name in ("KB_RR_CONFIG", "KB_POOL_CONFIG"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "failed to read RR endpoint config"):
                kb_api.rr_configuration({name: str(self.root / "missing.json"),
                                         "KB_RR_BASE_URL": "https://explicit.invalid", "KB_RR_KEY_FILE": str(self.key)})

    def test_errors_name_the_rr_endpoint(self):
        with self.assertRaises(ValueError) as caught:
            kb_api.rr_configuration({})
        self.assertIn("RR endpoint", str(caught.exception))
        self.assertNotIn("pool", str(caught.exception).lower())

    def test_cli_options_override_environment(self):
        self.write_config()
        results = []
        resolve = kb_api.rr_configuration

        class StopBeforeBuild(Exception):
            pass

        def inspect_configuration():
            results.append(resolve())
            raise StopBeforeBuild()

        for options, expected in (
                (["--rr-config", str(self.config), "--rr-base-url", "http://cli.invalid:9000/rr",
                  "--rr-key-file", str(self.key), "--rr-private-http"], "http://cli.invalid:9000/rr"),
                (["--pool-config", str(self.config), "--origin", "http://cli.invalid:9000",
                  "--key-file", str(self.key), "--private-http"], "http://cli.invalid:9000/_pool/rr")):
            results.clear()
            argv = ["kb_repo_url.py", ".", *options]
            with self.subTest(options=options), \
                    mock.patch.dict(os.environ, {"KB_RR_CONFIG": str(self.root / "wrong.json"),
                                                 "KB_RR_BASE_URL": "https://env.invalid", "KB_RR_KEY_FILE": "/missing",
                                                 "KB_RR_PRIVATE_HTTP": "0", "KB_POOL_ORIGIN": "https://old.invalid"},
                                    clear=True), \
                    mock.patch.object(kb_repo_url.sys, "argv", argv), \
                    mock.patch.object(kb_api, "rr_configuration", side_effect=inspect_configuration), \
                    self.assertRaises(StopBeforeBuild):
                kb_repo_url.main()
            self.assertEqual(results, [(expected, "test-key")])

    def test_missing_explicit_json_fails_during_planning_without_cloning(self):
        for option in ("--rr-config", "--pool-config"):
            for planning_flag in ("--dry-run", "--map-only"):
                argv = ["kb_repo_url.py", ".", option, str(self.root / "missing.json"), planning_flag]
                with self.subTest(option=option, flag=planning_flag), mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch.object(kb_repo_url.sys, "argv", argv), \
                        mock.patch.object(kb_repo_url, "clone_repository") as clone, \
                        self.assertRaisesRegex(ValueError, "failed to read RR endpoint config"):
                    kb_repo_url.main()
                clone.assert_not_called()

    def test_decrypt_uses_saved_build_args_unless_given_explicitly(self):
        self.write_config()
        legacy = self.write_legacy_config()
        saved = {"build_args": ["--workers", "12", "--pool-config", str(legacy)]}
        with mock.patch.dict(os.environ, {}, clear=True):
            kb_decrypt.apply_rr(saved, argparse.Namespace(rr_config=None, rr_base_url=None, origin=None,
                                                          rr_key_file=None, rr_private_http=False))
            self.assertEqual(kb_api.rr_configuration()[0], "https://legacy.invalid/_pool/rr")
        with mock.patch.dict(os.environ, {}, clear=True):
            kb_decrypt.apply_rr({"build_args": ["--origin", "https://saved.invalid", "--key-file", "/missing"]},
                                argparse.Namespace(rr_config=str(self.config), rr_base_url=None, origin=None,
                                                   rr_key_file=None, rr_private_http=False))
            self.assertEqual(kb_api.rr_configuration(), ("https://first.invalid/v1", "test-key"))

    def test_malformed_json_and_field_types_fail_without_exposing_contents(self):
        for payload in ([], {"origin": 123}, {"base_url": 1}, {"key_file": []}, {"state_dir": {}},
                        {"private_http": "false"}):
            self.config.write_text(json.dumps(payload))
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                kb_api.rr_configuration({"KB_RR_CONFIG": str(self.config)})


if __name__ == "__main__":
    unittest.main()
