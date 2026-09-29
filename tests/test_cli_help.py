import contextlib
import io
import unittest

import kb_cli


class CliHelpTest(unittest.TestCase):
    def test_root_help_prints_percent_in_decrypt_description(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            kb_cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("±2%以内のblobは打ち切る", output.getvalue())
