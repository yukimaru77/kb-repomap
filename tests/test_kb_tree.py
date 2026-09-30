import unittest

from kb_tree import render_tree


PATHS = ["gateway/main.go", "admin-ui/src/shared/foo.ts", "README.md",
         "admin-ui/package.json", "admin-ui/src/app.ts"]


class RenderTreeTests(unittest.TestCase):
    def test_directories_only_by_default(self):
        self.assertEqual(render_tree(PATHS), "\n".join([
            ".",
            "  admin-ui/",
            "    src/",
            "      shared/",
            "  gateway/",
        ]) + "\n")

    def test_files_sorted_and_indented_under_root_when_requested(self):
        self.assertEqual(render_tree(PATHS, include_files=True), "\n".join([
            ".",
            "  README.md",
            "  admin-ui/",
            "    package.json",
            "    src/",
            "      app.ts",
            "      shared/",
            "        foo.ts",
            "  gateway/",
            "    main.go",
        ]) + "\n")

    def test_order_does_not_depend_on_input_and_duplicates_collapse(self):
        paths = ["b/x", "a/y", "a/x", "c"]
        for include_files in (False, True):
            with self.subTest(include_files=include_files):
                self.assertEqual(render_tree(paths, include_files),
                                 render_tree(list(reversed(paths)) + ["a/x"], include_files))

    def test_empty_or_top_level_files_only_is_the_root(self):
        self.assertEqual(render_tree([]), ".\n")
        self.assertEqual(render_tree(["README.md"]), ".\n")
        self.assertEqual(render_tree(["README.md"], include_files=True), ".\n  README.md\n")


if __name__ == "__main__":
    unittest.main()
