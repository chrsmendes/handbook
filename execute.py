from handbook.downloader import ArchiveUnavailable, HandbookDownloader
from handbook.sanitizer import LinkSanitizer
from handbook.filler import FillMissing
from handbook import const
from handbook.preservation import CaptureWorker, seed_queue
import argparse
import datetime
import json
import os
from pathlib import Path


def download(
    dir: Path, language=const.DEFAULT_LANGUAGE, refresh=False, capture_root=None
):
    date = dir.stem
    print(f"------------ Starting {date} ------------")

    downloader = HandbookDownloader(dir, language=language)
    success = True
    for page in const.PAGES:
        try:
            if not downloader.get_page(date, page, refresh=refresh):
                success = False
        except ArchiveUnavailable:
            raise
        except Exception as e:
            print(f"----Failed to download {page}")
            print(e)
            success = False
        else:
            if (
                capture_root is not None
                and date == const.DATES[-1]
                and os.environ.get("IA_ACCESS_KEY")
                and os.environ.get("IA_SECRET_KEY")
            ):
                # Submit and follow work during the download pass, respecting queue limits.
                CaptureWorker(capture_root, [dir]).run(max_seconds=10)
    return success


def sanitize(dir: Path, rm_links: bool):
    date = dir.stem
    print(f"------------ Sanitizing {date} ------------")

    sanitizer = LinkSanitizer(dir, rm_links)
    sanitizer.run()


def missing(dir: Path, fill: bool, language=const.DEFAULT_LANGUAGE):
    missing = FillMissing(dir)
    dates = const.DATES
    for date in dates:
        print(f"------------ Finding missing {date} ------------")
        missing.find_missing(date, verbose=language == "eng")

    print()
    perc = 100 * len(missing.missing) / (len(dates) * len(const.PAGES)) if dates else 0
    print()
    print(f"Missing {perc:.2f}% of links.")
    missing.save_missing(append=language == "eng")

    if fill:
        missing.fill()


def save_download_status(root: Path, report: dict):
    """Record archive outages separately from unavailable individual captures."""
    for date, entry in report.items():
        entry["downloaded"] = sum(
            (root / date / f"{page}.md").is_file() for page in const.PAGES
        )
        entry["expected"] = len(const.PAGES)
    temporary = root / "download-status.json.tmp"
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    temporary.replace(root / "download-status.json")


def preserve(base: Path, languages, dates, max_seconds=300):
    directories = []
    for language in languages:
        root = base if language == "eng" else base / language
        for date in dates:
            directory = root / date
            if directory.is_dir() and (directory / "sources.json").exists():
                seed_queue(directory)
                if (directory / "capture-queue.json").exists():
                    directories.append(directory)
    if not directories:
        return True
    return CaptureWorker(base, directories).run(max_seconds=max_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dir", default=Path("editions"), type=Path, help="Directory to save data."
    )
    parser.add_argument(
        "--lang",
        default=const.DEFAULT_LANGUAGE,
        choices=const.LANGUAGES,
        help="Source language (default: eng). Portuguese files use a por/ subdirectory.",
    )
    subparsers = parser.add_subparsers(
        help="Type of operation to perform", dest="operation"
    )

    # ------------------------- Downloader ------------------------- #
    download_parser = subparsers.add_parser("download")
    download_parser.add_argument(
        "--date",
        default=const.DATES[-1],
        choices=const.DATES + ["all"],
        help="Edition to download (YYYY-MM). Defaults to the latest configured edition.",
    )
    download_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Download existing files again.",
    )
    download_parser.add_argument(
        "--archive-budget",
        type=float,
        default=300,
        help="Maximum seconds spent following Save Page Now jobs after download (default: 300).",
    )

    archive_parser = subparsers.add_parser(
        "archive", help="Resume Save Page Now jobs without downloading chapters."
    )
    archive_parser.add_argument("--date", default="all", choices=const.DATES + ["all"])
    archive_parser.add_argument("--all-languages", action="store_true")
    archive_parser.add_argument("--max-seconds", type=float, default=300)

    # ------------------------- Filler ------------------------- #
    missing_parser = subparsers.add_parser("missing")
    missing_parser.add_argument(
        "--fill",
        action="store_true",
        help="If missing files should be filled. Otherwise creates a file of missing links.",
    )

    # ------------------------- Sanitizer ------------------------- #
    sanitize_parser = subparsers.add_parser("sanitize")
    sanitize_parser.add_argument(
        "--date",
        default=const.DATES[-1],
        choices=const.DATES + ["all"],
        help="Edition to sanitize (YYYY-MM). Defaults to the latest configured edition.",
    )
    sanitize_parser.add_argument(
        "--rm-links", action="store_true", help="Remove links."
    )

    args = parser.parse_args()
    base = args.dir
    if args.operation == "archive":
        languages = const.LANGUAGES if args.all_languages else [args.lang]
        dates = const.DATES if args.date == "all" else [args.date]
        if args.max_seconds < 0:
            parser.error("--max-seconds must be nonnegative")
        raise SystemExit(0 if preserve(base, languages, dates, args.max_seconds) else 1)
    if args.operation == "download" and args.archive_budget < 0:
        parser.error("--archive-budget must be nonnegative")
    if args.lang != "eng":
        args.dir = args.dir / args.lang
        if args.operation == "missing" and args.fill:
            parser.error(
                "--fill is only supported for legacy English editions; Portuguese gaps remain explicit."
            )

    # ------------------------- Run things ------------------------- #
    args.dir.mkdir(parents=True, exist_ok=True)

    if args.operation == "missing":
        missing(args.dir, args.fill, args.lang)

    elif args.operation == "download":
        if args.date == "all":
            dates = const.DATES[::-1]
        else:
            dates = [args.date]

        success = True
        report_file = args.dir / "download-status.json"
        report = (
            json.loads(report_file.read_text(encoding="utf8"))
            if report_file.exists()
            else {}
        )
        if args.lang != "eng":
            for date in dates:
                report.setdefault(date, {"status": "pending"})
            save_download_status(args.dir, report)
        for d in dates:
            try:
                complete = download(
                    args.dir / d, args.lang, args.refresh, capture_root=base
                )
            except ArchiveUnavailable as error:
                print(f"----Stopping archive requests: {error}")
                if args.lang != "eng":
                    report[d] = {
                        "status": "blocked",
                        "attempted_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                        "error": str(error),
                    }
                    save_download_status(args.dir, report)
                success = False
                break
            success = complete and success
            if args.lang != "eng":
                report[d] = {
                    "status": "complete" if complete else "incomplete",
                    "attempted_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                }
                save_download_status(args.dir, report)
        if args.lang != "eng":
            missing(args.dir, False, args.lang)
        preserve(base, [args.lang], dates, args.archive_budget)
        if not success:
            raise SystemExit(1)

    elif args.operation == "sanitize":
        if args.date == "all":
            dates = const.DATES[::-1]
            if args.lang != "eng":
                dates = [d for d in dates if (args.dir / d).is_dir()]
        else:
            dates = [args.date]

        for d in dates:
            sanitize(args.dir / d, args.rm_links)
