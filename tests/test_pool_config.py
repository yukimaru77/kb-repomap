import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import kb_api
import kb_repo_url
from kb_remote import RemoteKB


class PoolConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "bridge.json"
        self.key = self.root / "keys" / "client.key"
        self.key.parent.mkdir()
        self.key.write_text("test-key\n")

    def write_config(self, **settings):
        self.config.write_text(json.dumps({"origin": "https://first.invalid", "key_file": "keys/client.key", **settings}))
        return {"KB_POOL_CONFIG": str(self.config)}

    def test_shared_json_relative_key_and_origin_changes_are_read_each_time(self):
        environ = self.write_config()
        self.assertEqual(kb_api.pool_configuration(environ), ("https://first.invalid/_pool/rr", "test-key"))
        self.write_config(origin="https://changed.invalid/")
        self.assertEqual(kb_api.pool_configuration(environ), ("https://changed.invalid/_pool/rr", "test-key"))
        self.assertEqual(environ, {"KB_POOL_CONFIG": str(self.config)})

    def test_state_directory_fallback_and_home_expansion(self):
        for settings, key_path in (({}, self.root / "state" / "client.key"),
                                   ({"state_dir": "alternate"}, self.root / "alternate" / "client.key"),
                                   ({"state_dir": "~/pool-state"}, self.root / "pool-state" / "client.key"),
                                   ({"key_file": "~/home-key"}, self.root / "home-key")):
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text("fallback-key")
            self.config.write_text(json.dumps({"origin": "http://localhost:8000", **settings}))
            with self.subTest(settings=settings), mock.patch.dict(os.environ, {"HOME": str(self.root)}):
                self.assertEqual(kb_api.pool_configuration({"KB_POOL_CONFIG": "~/bridge.json"}),
                                 ("http://localhost:8000/_pool/rr", "fallback-key"))

    def test_explicit_environment_overrides_json_including_private_http_false(self):
        environ = self.write_config(origin="http://private.invalid:18473", private_http=True)
        self.assertEqual(kb_api.pool_configuration(environ)[0], "http://private.invalid:18473/_pool/rr")
        with self.assertRaisesRegex(ValueError, "remote HTTP"):
            kb_api.pool_configuration({**environ, "KB_POOL_PRIVATE_HTTP": "0"})
        override_key = self.root / "override.key"
        override_key.write_text("override-key")
        self.assertEqual(kb_api.pool_configuration({**environ, "KB_POOL_ORIGIN": "https://explicit.invalid",
                         "KB_POOL_KEY_FILE": str(override_key), "KB_POOL_PRIVATE_HTTP": "0"}),
                         ("https://explicit.invalid/_pool/rr", "override-key"))

    def test_missing_explicit_json_is_an_error_even_when_fields_are_overridden(self):
        with self.assertRaisesRegex(ValueError, "failed to read pool config"):
            kb_api.pool_configuration({"KB_POOL_CONFIG": str(self.root / "missing.json"),
                                       "KB_POOL_ORIGIN": "https://explicit.invalid", "KB_POOL_KEY_FILE": str(self.key)})

    def test_cli_pool_config_and_fields_override_environment(self):
        self.write_config()
        results = []
        resolve = kb_api.pool_configuration

        class StopBeforeBuild(Exception):
            pass

        def inspect_configuration():
            results.append(resolve())
            raise StopBeforeBuild()

        argv = ["kb_repo_url.py", ".", "--pool-config", str(self.config),
                "--origin", "http://cli.invalid:9000", "--key-file", str(self.key), "--private-http"]
        with mock.patch.dict(os.environ, {"KB_POOL_CONFIG": str(self.root / "wrong.json"),
                                          "KB_POOL_ORIGIN": "https://env.invalid", "KB_POOL_KEY_FILE": "/missing",
                                          "KB_POOL_PRIVATE_HTTP": "0"}, clear=True), \
                mock.patch.object(kb_repo_url.sys, "argv", argv), \
                mock.patch.object(kb_api, "pool_configuration", side_effect=inspect_configuration), \
                self.assertRaises(StopBeforeBuild):
            kb_repo_url.main()
        self.assertEqual(results, [("http://cli.invalid:9000/_pool/rr", "test-key")])

    def test_remote_registration_uses_shared_json_and_wrapper_overrides(self):
        self.write_config()
        snapshot = self.root / "kb.json"
        snapshot.write_text('[{"type":"compaction","encrypted_content":"blob"}]')
        config = {"build_args": ["--workers", "12", "--pool-config", str(self.config)]}
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(RemoteKB(config, snapshot).origin, "https://first.invalid")
            self.write_config(origin="http://changed.invalid", private_http=True)
            self.assertEqual(RemoteKB(config, snapshot).origin, "http://changed.invalid")
        other_key = self.root / "other.key"
        other_key.write_text("wrapper-key")
        with mock.patch.dict(os.environ, {"KB_POOL_ORIGIN": "http://127.0.0.1:9001",
                                         "KB_POOL_KEY_FILE": str(other_key), "KB_POOL_PRIVATE_HTTP": "0"}, clear=True):
            remote = RemoteKB(config, snapshot)
        self.assertEqual((remote.origin, remote.key), ("http://127.0.0.1:9001", "wrapper-key"))

    def test_missing_explicit_json_fails_during_planning_without_cloning(self):
        for planning_flag in ("--dry-run", "--map-only"):
            argv = ["kb_repo_url.py", ".", "--pool-config", str(self.root / "missing.json"), planning_flag]
            with self.subTest(flag=planning_flag), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(kb_repo_url.sys, "argv", argv), \
                    mock.patch.object(kb_repo_url, "clone_repository") as clone, \
                    self.assertRaisesRegex(ValueError, "failed to read pool config"):
                kb_repo_url.main()
            clone.assert_not_called()

    def test_remote_build_fields_override_json_defaults(self):
        self.write_config(origin="https://first.invalid", key_file="missing-key")
        snapshot = self.root / "kb.json"
        snapshot.write_text('[{"type":"compaction","encrypted_content":"blob"}]')
        with mock.patch.dict(os.environ, {}, clear=True):
            remote = RemoteKB({"build_args": ["--pool-config", str(self.config), "--origin", "http://private.invalid",
                                               "--key-file", str(self.key), "--private-http"]}, snapshot)
        self.assertEqual((remote.origin, remote.key), ("http://private.invalid", "test-key"))

    def test_malformed_json_and_field_types_fail_without_exposing_contents(self):
        for payload in ([], {"origin": 123}, {"key_file": []}, {"state_dir": {}}, {"private_http": "false"}):
            self.config.write_text(json.dumps(payload))
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                kb_api.pool_configuration({"KB_POOL_CONFIG": str(self.config)})
