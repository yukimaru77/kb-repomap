from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import kb_cli
import kb_developer


class PiLaunchTest(unittest.TestCase):
    def test_client_boundary_finds_pi(self):
        self.assertEqual(kb_developer.client_boundary(["octane", "--store", "pi", "pi", "-c"]), 3)

    def test_kb_name_pi_execs_pi_with_autoload_and_passes_pi_args(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(kb_cli.os, "execvpe") as execvpe, mock.patch.object(kb_cli.os, "chdir") as chdir:
            kb_cli.main(["octane", "--store", "pepabo", "--file", "latest.json", "--workspace", temp,
                         "pi", "--model", "sonnet", "hello"])
        command, args, env = execvpe.call_args.args
        self.assertEqual(command, "pi")
        self.assertEqual(args, ["pi", "--model", "sonnet", "hello"])
        self.assertEqual(env["PI_KB_AUTOLOAD"], "octane --store pepabo --file latest")
        chdir.assert_called_once_with(Path(temp).resolve())

    def test_pi_help_does_not_autoload(self):
        with mock.patch.object(kb_cli.os, "execvp") as execvp, mock.patch.object(kb_cli.os, "execvpe") as execvpe:
            kb_cli.main(["octane", "pi", "--help"])
        execvp.assert_called_once_with("pi", ["pi", "--help"])
        execvpe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
