"""Sync/preserve editions, commit each language/date separately, optionally publish."""

import argparse
import os
from pathlib import Path
import subprocess

from . import const
from .downloader import HandbookDownloader
from .history import build_history, edition_directories, git
from .preservation import CaptureWorker, seed_queue
from .sanitizer import LinkSanitizer


def commit_path(root, path, message):
    relative = path.relative_to(root).as_posix()
    if not git(root, "status", "--porcelain", "--", relative):
        return False
    git(root, "add", "--", relative)
    if git(root, "diff", "--cached", "--quiet", check=False) is None:
        git(root, "commit", "-m", message)
        return True
    return False


def commit_editions(root, languages):
    if git(root, "diff", "--cached", "--name-only"):
        raise ValueError(
            "The index contains staged changes; refusing to mix them into edition commits"
        )
    for language in languages:
        for directory in edition_directories(root, language):
            commit_path(
                root, directory, f"Update {language} handbook edition {directory.name}"
            )
    # Control files contain no chapter text and have their own bookkeeping commits.
    state = root / "editions" / ".wayback-state.json"
    if state.exists():
        commit_path(root, state, "Update Wayback worker scheduling state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["sync", "resume"], default="resume")
    parser.add_argument("--language", choices=[*const.LANGUAGES, "all"], default="all")
    parser.add_argument("--date", choices=[*const.DATES, "latest"], default="latest")
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.publish and not args.commit:
        parser.error("--publish requires --commit")
    if args.max_seconds < 0:
        parser.error("--max-seconds must be nonnegative")
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
    languages = const.LANGUAGES if args.language == "all" else [args.language]
    date = const.DATES[-1] if args.date == "latest" else args.date
    if args.mode == "sync" and date != const.DATES[-1]:
        parser.error(
            "Historical recovery is separate: use execute.py download --date YYYY-MM"
        )
    if args.commit and git(root, "diff", "--cached", "--name-only"):
        parser.error("Commit or unstage existing changes before running --commit")
    if args.mode == "sync":
        for language in languages:
            base = (
                root / "editions" if language == "eng" else root / "editions" / language
            )
            downloader = HandbookDownloader(base / date, language)
            for page in const.PAGES:
                try:
                    downloader.get_page(date, page, refresh=True)
                except Exception as error:
                    print(
                        f"Download failed for {language}/{date}/{page}: {type(error).__name__}"
                    )
                else:
                    if os.environ.get("IA_ACCESS_KEY") and os.environ.get(
                        "IA_SECRET_KEY"
                    ):
                        CaptureWorker(root / "editions", [base / date]).run(
                            max_seconds=10
                        )
            LinkSanitizer(base / date, False).run()
    directories = []
    for language in languages:
        for directory in edition_directories(root, language):
            seed_queue(directory)
            if (directory / "capture-queue.json").exists():
                directories.append(directory)
    complete = (
        CaptureWorker(root / "editions", directories).run(max_seconds=args.max_seconds)
        if directories
        else True
    )
    if args.commit:
        commit_editions(root, languages)
    build_history(root, languages, args.repository)
    if args.commit:
        for language in languages:
            commit_path(
                root,
                root / "docs" / f"comparisons-{language}.md",
                f"Update {language} edition comparison links",
            )
    if args.publish:
        branch = git(root, "symbolic-ref", "--short", "HEAD")
        refs = [f"HEAD:refs/heads/{branch}"]
        for language in languages:
            refs.extend(
                git(
                    root,
                    "for-each-ref",
                    "--format=%(refname)",
                    f"refs/heads/diff-{language}",
                    f"refs/tags/{language}/",
                ).splitlines()
            )
        # No force flags, no legacy tag refspecs, and no partial publication.
        subprocess.run(
            ["git", "-C", str(root), "push", "--atomic", "origin", *refs], check=True
        )
    if not complete:
        print(
            "Downloads and pending captures were preserved; some Wayback jobs still need attention."
        )


if __name__ == "__main__":
    main()
