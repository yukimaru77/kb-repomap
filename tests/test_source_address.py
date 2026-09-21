import contextlib
import io
import unittest
from unittest import mock

import kb_cli
from kb_source import source_address
from kb_repo_url import validate_source


class SourceAddressTest(unittest.TestCase):
    def test_remote_absolute_paths_and_git_urls(self):
        self.assertEqual(source_address(host="user@box",path="/home/user/my repo"),"ssh://user@box/home/user/my%20repo")
        self.assertEqual(validate_source("box:/srv/repo.git"),"ssh://box/srv/repo.git")
        for url in ("https://github.com/owner/repo.git","git@github.com:owner/repo.git","ssh://box/srv/repo"):
            self.assertEqual(source_address(url),url)

    def test_invalid_or_ambiguous_remote_source(self):
        for kw in ({"host":"box","path":"relative"},{"host":"-oProxyCommand=bad","path":"/repo"},
                   {"value":"https://x/repo","host":"box","path":"/repo"},{"host":"box"}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):source_address(**kw)

    def test_register_machine_path_persists_git_url(self):
        cfg={"stores":[{"name":"research","url":"https://example.com/store.git"}]}
        with mock.patch.object(kb_cli.store,"read_config",return_value=cfg), \
             mock.patch.object(kb_cli.store,"publish",return_value="revision") as publish, \
             mock.patch("builtins.input",return_value=""),contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["register","example","--host","box","--path","/srv/code","--branch","main"])
        self.assertEqual(publish.call_args.args[2],{"repository_url":"ssh://box/srv/code","source_commit":None,"branch":"main"})
