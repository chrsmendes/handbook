"""Exercise real Git repositories: language isolation and immutable published refs."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handbook import const
from handbook.automation import commit_editions
from handbook.history import build_history, git
from handbook.preservation import content_hash, write_json

PAGE = const.PAGES[0]


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "History test")
        git(self.root, "config", "user.email", "history@example.invalid")
        (self.root / "README.md").write_text("Base repository\n")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-m", "Base")
        self.head = git(self.root, "rev-parse", "HEAD")
        pages = patch.object(const, "PAGES", [PAGE])
        pages.start()
        self.addCleanup(pages.stop)
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def edition(self, language, date, text):
        base = self.root / "editions"
        if language != "eng":
            base /= language
        directory = base / date
        directory.mkdir(parents=True, exist_ok=True)
        markdown = f"# 0. Introduction\n\n{text}\n"
        (directory / f"{PAGE}.md").write_text(markdown)
        if language == "por":
            write_json(
                directory / "sources.json",
                {
                    PAGE: {
                        "edition": date,
                        "language": language,
                        "content_sha256": content_hash(markdown),
                    }
                },
            )
        return directory

    def refs(self):
        return git(self.root, "for-each-ref", "--format=%(refname) %(objectname)")

    def build(self, languages=const.LANGUAGES, dry_run=False):
        build_history(self.root, languages, "example/handbook", dry_run)

    def test_separate_histories_preserve_legacy_tags_checkout_and_index(self):
        git(self.root, "tag", "2020-03")
        legacy = git(self.root, "rev-parse", "2020-03")
        (self.root / "unrelated.txt").write_text("Staged user change")
        git(self.root, "add", "unrelated.txt")
        staged = git(self.root, "diff", "--cached")
        for language in const.LANGUAGES:
            self.edition(language, "2026-03", f"{language} March")
            self.edition(language, "2026-10", f"{language} October")
        self.build()
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.head)
        self.assertEqual(git(self.root, "diff", "--cached"), staged)
        self.assertEqual(git(self.root, "rev-parse", "2020-03"), legacy)
        for language in const.LANGUAGES:
            self.assertEqual(
                git(self.root, "rev-list", "--count", f"diff-{language}"), "2"
            )
            for date in ("2026-03", "2026-10"):
                tag = f"{language}/{date}"
                self.assertEqual(git(self.root, "cat-file", "-t", tag), "tag")
                self.assertEqual(
                    git(self.root, "ls-tree", "-r", "--name-only", tag),
                    f"handbook/{PAGE}.md",
                )
                self.assertIn(
                    language, git(self.root, "show", f"{tag}:handbook/{PAGE}.md")
                )
        before = self.refs()
        self.build()
        self.assertEqual(self.refs(), before)

    def test_conflict_never_moves_tags_or_partially_creates_other_refs(self):
        self.edition("por", "2026-10", "Original October")
        self.build(["por"])
        before = self.refs()
        self.edition("por", "2026-03", "Recovered March")
        self.edition("por", "2026-10", "Changed October")
        with self.assertRaisesRegex(ValueError, "immutable tag por/2026-10"):
            self.build(["por"])
        self.assertEqual(self.refs(), before)
        self.assertIsNone(
            git(
                self.root, "rev-parse", "--verify", "refs/tags/por/2026-03", check=False
            )
        )

    def test_backfill_keeps_published_tags_and_two_dot_comparisons(self):
        self.edition("por", "2026-10", "October text")
        self.build(["por"])
        latest = git(self.root, "rev-parse", "por/2026-10")
        self.edition("por", "2026-03", "March text")
        self.build(["por"])
        self.assertEqual(git(self.root, "rev-parse", "por/2026-10"), latest)
        index = (self.root / "docs/comparisons-por.md").read_text()
        self.assertIn("por%2F2026-03..por%2F2026-10", index)
        diff = git(self.root, "diff", "por/2026-03..por/2026-10")
        self.assertIn("-March text", diff)
        self.assertIn("+October text", diff)

    def test_incomplete_edition_is_not_tagged(self):
        self.edition("por", "2026-10", "One chapter")
        with patch.object(const, "PAGES", [PAGE, "1-work-of-salvation-and-exaltation"]):
            self.build(["por"])
        self.assertIsNone(
            git(
                self.root, "rev-parse", "--verify", "refs/tags/por/2026-10", check=False
            )
        )

    def test_dry_run_does_not_create_refs_or_documentation(self):
        self.edition("por", "2026-10", "October text")
        before = self.refs()
        self.build(["por"], dry_run=True)
        self.assertEqual(self.refs(), before)
        self.assertFalse((self.root / "docs").exists())

    def test_dataset_commits_contain_only_one_language_and_edition(self):
        paths = []
        for language in const.LANGUAGES:
            for date in ("2026-03", "2026-10"):
                paths.append(
                    self.edition(language, date, f"{language} {date}")
                    .relative_to(self.root)
                    .as_posix()
                )
        (self.root / "code.py").write_text("# unrelated code change\n")
        commit_editions(self.root, const.LANGUAGES)
        commits = git(self.root, "rev-list", f"{self.head}..HEAD").splitlines()
        self.assertEqual(len(commits), 4)
        for commit in commits:
            files = git(
                self.root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit
            ).splitlines()
            matched = [
                path
                for path in paths
                if all(file.startswith(path + "/") for file in files)
            ]
            self.assertEqual(len(matched), 1)
        self.assertEqual(
            git(self.root, "status", "--porcelain", "--", "code.py"), "?? code.py"
        )

    def test_dataset_commits_refuse_unrelated_staged_changes(self):
        self.edition("por", "2026-10", "October text")
        (self.root / "code.py").write_text("# staged code\n")
        git(self.root, "add", "code.py")
        with self.assertRaisesRegex(ValueError, "staged changes"):
            commit_editions(self.root, ["por"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.head)


if __name__ == "__main__":
    unittest.main()
