import unittest

from kb_tree import render_tree


class RenderTreeTests(unittest.TestCase):
    def test_sorted_indented_with_root_and_directory_suffix(self):
        paths = ["gateway/main.go", "admin-ui/src/shared/foo.ts", "README.md",
                 "admin-ui/package.json", "admin-ui/src/app.ts"]
        self.assertEqual(render_tree(paths), "\n".join([
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
        self.assertEqual(render_tree(paths), render_tree(list(reversed(paths)) + ["a/x"]))

    def test_empty_list_is_only_the_root(self):
        self.assertEqual(render_tree([]), ".\n")

    def test_limit_is_inclusive(self):
        full = render_tree(["a/b.txt"])
        self.assertEqual(render_tree(["a/b.txt"], max_chars=len(full)), full)

    def test_falls_back_to_directories_only_with_omitted_count(self):
        paths = ["admin-ui/src/foo.ts", "admin-ui/src/bar.ts", "gateway/main.go", "README.md"]
        self.assertEqual(render_tree(paths, max_chars=20), "\n".join([
            "(files omitted: 4 files; directories only)",
            "",
            ".",
            "  admin-ui/",
            "    src/",
            "  gateway/",
        ]) + "\n")

    def test_default_limit_is_sixty_thousand_characters(self):
        paths = [f"dir/{index:05d}-{'x' * 40}.txt" for index in range(2000)]
        self.assertGreater(len("\n".join(paths)), 60_000)
        self.assertTrue(render_tree(paths).startswith("(files omitted: 2000 files; directories only)\n\n.\n  dir/\n"))


if __name__ == "__main__":
    unittest.main()
