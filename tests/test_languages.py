"""Regression tests for source-language isolation and localized links."""

import contextlib
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from handbook import const
from handbook.downloader import HandbookDownloader
from handbook.sanitizer import LinkSanitizer

PAGE = "0-introductory-overview"
OFFICIAL = f"{const.PREFIX}{PAGE}?lang=por"
REPO = Path(__file__).resolve().parents[1]


def response(language="por", text="Texto oficial de exemplo.", url=OFFICIAL, page=PAGE):
    # Synthetic HTML models the attributes observed on the official chapter.
    result = Mock()
    result.status_code = 200
    result.url = url
    result.content = f"""<html lang="pt"><article data-content-type="chapter"
        lang="{language}" data-uri="/manual/general-handbook/{page}">
        <div class="body"><header><p>0.</p><h1>Visão geral introdutória</h1></header>
        <div class="body-block"><p>0.2</p><h2>Adaptação e recursos opcionais</h2>
        <p>{text}</p></div></div></article></html>""".encode()
    return result


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "por" / const.DATES[-1]
        self.downloader = HandbookDownloader(self.directory, language="por")
        self.output = self.directory / f"{PAGE}.md"
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        sleeper = patch("handbook.downloader.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    @patch("handbook.downloader.requests.get")
    def test_official_portuguese_and_provenance(self, get):
        get.return_value = response()
        self.assertTrue(self.downloader.get_page(const.DATES[-1], PAGE))
        get.assert_called_once_with(OFFICIAL, timeout=const.REQUEST_TIMEOUT)
        self.assertIn("# 0. Visão geral introdutória", self.output.read_text())
        sources = json.loads((self.directory / "sources.json").read_text())
        self.assertEqual(sources[PAGE]["language"], "por")
        self.assertEqual(sources[PAGE]["official_url"], OFFICIAL)
        self.assertEqual(len(sources[PAGE]["html_sha256"]), 64)

    @patch("handbook.downloader.requests.get")
    def test_transient_connection_failure_retries_the_same_official_source(self, get):
        get.side_effect = [requests.Timeout("timeout"), response()]
        self.assertTrue(self.downloader.get_page(const.DATES[-1], PAGE))
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            self.assertEqual(call.args[0], OFFICIAL)
        self.assertIn("Texto oficial", self.output.read_text())

    @patch("handbook.downloader.requests.get")
    def test_transient_server_failure_retries_without_changing_source(self, get):
        unavailable = response()
        unavailable.status_code = 503
        unavailable.raise_for_status.side_effect = requests.HTTPError("503")
        get.side_effect = [unavailable, response()]
        self.assertTrue(self.downloader.get_page(const.DATES[-1], PAGE))
        self.assertEqual(get.call_count, 2)
        self.assertIn("Texto oficial", self.output.read_text())

    @patch("handbook.downloader.requests.get")
    def test_refresh_updates_existing_dated_edition(self, get):
        get.side_effect = [
            response(text="Primeira versão."),
            response(text="Segunda versão."),
        ]
        self.downloader.get_page(const.DATES[-1], PAGE)
        self.downloader.get_page(const.DATES[-1], PAGE, refresh=True)
        self.assertEqual(get.call_count, 2)
        self.assertIn("Segunda versão.", self.output.read_text())
        self.assertNotIn("Primeira versão.", self.output.read_text())

    @patch("handbook.downloader.requests.get")
    def test_bad_responses_preserve_existing_chapter_and_source(self, get):
        get.return_value = response()
        self.downloader.get_page(const.DATES[-1], PAGE)
        original = self.output.read_bytes()
        sources = (self.directory / "sources.json").read_bytes()
        bad = [
            response(language="eng"),  # Portuguese shell, English chapter.
            response(url=OFFICIAL.replace("lang=por", "lang=eng")),
            response(url="https://example.com/chapter?lang=por"),
            response(page="11-young-women"),
        ]
        empty = response()
        empty.content = b'<article data-content-type="chapter" lang="por" data-uri="/manual/general-handbook/0-introductory-overview"></article>'
        bad.append(empty)
        http_error = response()
        http_error.raise_for_status.side_effect = requests.HTTPError("503")
        bad.append(http_error)
        for item in bad:
            with self.subTest(url=item.url):
                get.return_value = item
                with self.assertRaises((ValueError, requests.HTTPError)):
                    self.downloader.get_page(const.DATES[-1], PAGE, refresh=True)
                self.assertEqual(self.output.read_bytes(), original)
                self.assertEqual(
                    (self.directory / "sources.json").read_bytes(), sources
                )
        get.side_effect = requests.Timeout("timeout")
        with self.assertRaises(requests.Timeout):
            self.downloader.get_page(const.DATES[-1], PAGE, refresh=True)
        self.assertEqual(self.output.read_bytes(), original)

    @patch("handbook.downloader.requests.get")
    def test_existing_unverified_history_is_not_silently_reused(self, get):
        self.output.write_text("English legacy text")
        with self.assertRaisesRegex(ValueError, "no verified por source"):
            self.downloader.get_page("2026-03", PAGE)
        get.assert_not_called()

    @patch("handbook.downloader.requests.get")
    def test_english_defaults_and_legacy_cache_remain(self, get):
        english = HandbookDownloader(Path(self.temp.name) / "2026-10")
        self.assertEqual(english.language, "eng")
        url, _ = english.find_link(const.DATES[-1], PAGE)
        self.assertEqual(url, OFFICIAL.replace("por", "eng"))
        file = english.dir / f"{PAGE}.md"
        file.write_text("Existing English chapter")
        self.assertTrue(english.get_page(const.DATES[-1], PAGE))
        self.assertEqual(file.read_text(), "Existing English chapter")
        get.assert_not_called()

    @patch("handbook.downloader.requests.get")
    def test_portuguese_dated_capture_uses_archive_with_language(self, get):
        stamp = "20260319090000"
        url = f"https://web.archive.org/web/{stamp}/{OFFICIAL}"
        get.return_value.json.return_value = {
            "archived_snapshots": {
                "closest": {
                    "timestamp": stamp,
                    "url": url,
                    "available": True,
                    "status": "200",
                }
            }
        }
        found, _ = self.downloader.find_link("2026-03", PAGE)
        self.assertEqual(found, f"https://web.archive.org/web/{stamp}id_/{OFFICIAL}")
        self.assertEqual(get.call_args.kwargs["params"]["url"], OFFICIAL)
        self.assertEqual(get.call_args.args[0], const.WAYBACK_API)

    @patch("handbook.downloader.requests.get")
    def test_latest_portuguese_edition_uses_official_live_source(self, get):
        self.assertEqual(
            self.downloader.find_link(const.DATES[-1], PAGE), (OFFICIAL, 0.1)
        )
        get.assert_not_called()

    @patch("handbook.downloader.requests.get")
    def test_snapshot_outside_interval_is_not_substituted(self, get):
        get.return_value.json.return_value = {
            "archived_snapshots": {
                "closest": {
                    "timestamp": "20260228000000",
                    "available": True,
                    "status": "200",
                    "url": f"https://web.archive.org/web/20260228000000/{OFFICIAL}",
                }
            }
        }
        self.assertFalse(self.downloader.get_page("2026-03", PAGE))
        self.assertFalse(self.output.exists())

    @patch("handbook.downloader.requests.get")
    def test_archive_redirect_to_other_capture_is_rejected(self, get):
        requested = f"https://web.archive.org/web/20260319090000id_/{OFFICIAL}"
        returned = f"https://web.archive.org/web/20261001000000id_/{OFFICIAL}"
        get.return_value = response(url=returned)
        with patch.object(self.downloader, "find_link", return_value=(requested, 0)):
            with self.assertRaisesRegex(ValueError, "different capture"):
                self.downloader.get_page("2026-03", PAGE)
        self.assertFalse(self.output.exists())

    def test_localized_links_and_provenance_survive_sanitizing_twice(self):
        intro = (
            "# 0. Visão geral introdutória\n\n## 0.2 Adaptação e recursos opcionais\n"
        )
        self.output.write_text(intro)
        chapter = self.directory / "11-young-women.md"
        chapter.write_text(
            "# 11 Moças\n\n"
            f"[[AO]](/study/manual/general-handbook/{PAGE}?lang=por&id=title2)\n"
            f"[0.2]({OFFICIAL}&id=title2)\n"
            "[38.1](/study/manual/general-handbook/38-church-policies-and-guidelines?lang=por)\n"
            "[Escritura](/study/scriptures/nt/john/1?lang=por)\n"
        )
        metadata = self.directory / "sources.json"
        metadata.write_text('{"language": "por"}\n')
        LinkSanitizer(self.directory, False).run()
        once = chapter.read_text()
        self.assertIn(f"[[AO]]({PAGE}.md#02-adaptação-e-recursos-opcionais)", once)
        self.assertIn(f"[0.2]({PAGE}.md#02-adaptação-e-recursos-opcionais)", once)
        self.assertIn(
            "https://www.churchofjesuschrist.org/study/manual/general-handbook/38-",
            once,
        )
        self.assertIn(
            "https://www.churchofjesuschrist.org/study/scriptures/nt/john/1?lang=por",
            once,
        )
        LinkSanitizer(self.directory, False).run()
        self.assertEqual(chapter.read_text(), once)
        self.assertEqual(metadata.read_text(), '{"language": "por"}\n')

    def test_cli_rejects_portuguese_fill(self):
        with patch(
            "sys.argv",
            [
                "execute.py",
                "--dir",
                self.temp.name,
                "--lang",
                "por",
                "missing",
                "--fill",
            ],
        ):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
                SystemExit
            ) as caught:
                runpy.run_path(str(REPO / "execute.py"), run_name="__main__")
        self.assertEqual(caught.exception.code, 2)

    @patch("handbook.downloader.requests.get")
    def test_dated_portuguese_edition_is_reused_without_refresh(self, get):
        get.return_value = response()
        self.downloader.get_page(const.DATES[-1], PAGE)
        self.downloader.get_page(const.DATES[-1], PAGE)
        self.assertEqual(get.call_count, 1)
        sources = json.loads((self.directory / "sources.json").read_text())
        self.assertEqual(sources[PAGE]["edition"], const.DATES[-1])

    @patch("handbook.downloader.requests.get")
    def test_archive_outage_stops_batch_and_can_be_resumed(self, get):
        limited = response()
        limited.status_code = 429
        limited.raise_for_status.side_effect = requests.HTTPError(
            "429 Too Many Requests"
        )
        get.side_effect = [response(), limited]
        args = [
            "execute.py",
            "--dir",
            self.temp.name,
            "--lang",
            "por",
            "download",
            "--date",
        ]
        dates = ["2025-12", "2026-03", "2026-10"]
        with patch.object(const, "PAGES", [PAGE]), patch.object(const, "DATES", dates):
            with patch("sys.argv", args + ["all"]), self.assertRaises(
                SystemExit
            ) as caught:
                runpy.run_path(str(REPO / "execute.py"), run_name="__main__")
            self.assertEqual(caught.exception.code, 1)
            self.assertEqual(get.call_count, 2)
            report_file = self.directory.parent / "download-status.json"
            report = json.loads(report_file.read_text())
            self.assertEqual(report["2026-10"]["status"], "complete")
            self.assertEqual(report["2026-03"]["status"], "blocked")
            self.assertIn("429", report["2026-03"]["error"])
            self.assertEqual(report["2025-12"]["status"], "pending")
            self.assertEqual(report["2026-03"]["downloaded"], 0)

            stamp = "20260319090000"
            archived_url = f"https://web.archive.org/web/{stamp}id_/{OFFICIAL}"
            availability = response()
            availability.json.return_value = {
                "archived_snapshots": {
                    "closest": {
                        "timestamp": stamp,
                        "url": archived_url,
                        "available": True,
                        "status": "200",
                    }
                }
            }
            get.side_effect = [availability, response(url=archived_url)]
            with patch("sys.argv", args + ["2026-03"]):
                runpy.run_path(str(REPO / "execute.py"), run_name="__main__")
            report = json.loads(report_file.read_text())
            self.assertEqual(report["2026-03"]["status"], "complete")
            self.assertNotIn("error", report["2026-03"])
            self.assertEqual(report["2025-12"]["status"], "pending")
            source = json.loads(
                (self.directory.parent / "2026-03" / "sources.json").read_text()
            )[PAGE]
            self.assertEqual(source["edition"], "2026-03")
            self.assertEqual(source["retrieved_url"], archived_url)

    @patch("handbook.downloader.requests.get")
    def test_cli_isolates_portuguese_from_existing_english(self, get):
        english = Path(self.temp.name) / const.DATES[-1] / f"{PAGE}.md"
        english.parent.mkdir()
        english.write_text("English stays here")
        get.return_value = response()
        with patch.object(const, "PAGES", [PAGE]):
            for operation in ("download", "sanitize", "missing", "missing"):
                with patch(
                    "sys.argv",
                    ["execute.py", "--dir", self.temp.name, "--lang", "por", operation],
                ):
                    runpy.run_path(str(REPO / "execute.py"), run_name="__main__")
        self.assertIn("Texto oficial", self.output.read_text())
        self.assertEqual(english.read_text(), "English stays here")
        missing = (self.directory.parent / "missing.txt").read_text().splitlines()
        self.assertEqual(len(missing), len(const.DATES) - 1)
        self.assertNotIn(str(self.output), missing)
        self.assertFalse((self.directory.parent / "current").exists())
        self.assertEqual(
            json.loads((self.directory.parent / "download-status.json").read_text())[
                const.DATES[-1]
            ]["status"],
            "complete",
        )


if __name__ == "__main__":
    unittest.main()
