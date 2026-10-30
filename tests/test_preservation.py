"""State-machine regressions for independent capture verification and resumption."""

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from handbook import const
from handbook.downloader import HandbookDownloader
from handbook.preservation import CaptureWorker, read_json, write_json

PAGE = const.PAGES[0]


def html(language="por", text="Official example text.", page=PAGE):
    return f"""<article data-content-type="chapter" lang="{language}"
    data-uri="/manual/general-handbook/{page}"><div class="body">
    <header><p>0.</p><h1>Introduction</h1></header>
    <div class="body-block"><p>{text}</p></div></div></article>""".encode()


def reply(url, status=200, payload=None, body=None, headers=None):
    response = requests.Response()
    response.url = url
    response.status_code = status
    response._content = body if body is not None else json.dumps(payload or {}).encode()
    response.headers.update(headers or {})
    return response


class Clock:
    def __init__(self):
        self.value = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc).timestamp()

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Session:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.stamp = "20261029120000"
        self.directory = self.root / "por" / const.DATES[-1]
        self.language = "por"
        self.raw = html()
        self.official = f"{const.PREFIX}{PAGE}?lang=por"
        with contextlib.redirect_stdout(io.StringIO()):
            self.seed()
        credentials = patch.dict(
            os.environ, {"IA_ACCESS_KEY": "test-access", "IA_SECRET_KEY": "test-secret"}
        )
        credentials.start()
        self.addCleanup(credentials.stop)
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def seed(self):
        downloader = HandbookDownloader(self.directory, self.language)
        with patch(
            "handbook.downloader.get_response",
            return_value=reply(self.official, body=self.raw),
        ), patch("handbook.downloader.sleep"):
            downloader.get_page(const.DATES[-1], PAGE, refresh=True)

    def worker(self, handler):
        self.session = Session(handler)
        return CaptureWorker(
            self.root,
            [self.directory],
            clock=self.clock,
            sleep=self.clock.sleep,
            session=self.session,
        )

    def successful(self, method, url, kwargs):
        if method == "POST":
            self.assertEqual(kwargs["data"]["url"], self.official)
            self.assertEqual(
                parse_qs(urlsplit(kwargs["data"]["url"]).query)["lang"], [self.language]
            )
            self.assertEqual(
                kwargs["headers"]["Authorization"], "LOW test-access:test-secret"
            )
            self.assertFalse(kwargs["allow_redirects"])
            return reply(url, payload={"job_id": "job-1"})
        if "/save/status/" in url:
            return reply(
                url,
                payload={
                    "status": "success",
                    "timestamp": self.stamp,
                    "original_url": self.official,
                },
            )
        self.assertNotIn("Authorization", kwargs["headers"])
        return reply(url, body=self.raw)

    def item(self):
        return next(iter(read_json(self.directory / "capture-queue.json").values()))

    def source(self):
        return read_json(self.directory / "sources.json")[PAGE]

    def assert_no_association(self):
        self.assertNotEqual(self.source()["wayback"]["status"], "verified")
        self.assertNotIn("url", self.source()["wayback"])
        self.assertNotIn("timestamp", self.source()["wayback"])

    def test_capture_verified_and_deduplicated_for_both_languages(self):
        for language in const.LANGUAGES:
            with self.subTest(language=language):
                self.language = language
                self.directory = self.root / language / const.DATES[-1]
                self.official = f"{const.PREFIX}{PAGE}?lang={language}"
                self.raw = html(language)
                self.seed()
                worker = self.worker(self.successful)
                self.assertTrue(worker.run(120))
                self.assertEqual(self.item()["status"], "verified")
                self.assertEqual(self.source()["wayback"]["timestamp"], self.stamp)
                self.assertEqual(
                    self.source()["wayback"]["url"],
                    f"https://web.archive.org/web/{self.stamp}/{self.official}",
                )
                self.seed()  # same edition + content must not be submitted again
                self.assertTrue(worker.run(120))
                self.assertEqual(
                    sum(method == "POST" for method, _, _ in self.session.calls), 1
                )
                self.assertEqual(
                    len(read_json(self.directory / "capture-queue.json")), 1
                )
                for path in self.directory.glob("*.json"):
                    self.assertNotIn("test-secret", path.read_text())
                    self.assertNotIn("test-access", path.read_text())

    def test_missing_credentials_keeps_download_and_pending_job(self):
        before = (self.directory / f"{PAGE}.md").read_bytes()
        with patch.dict(os.environ, {"IA_ACCESS_KEY": "", "IA_SECRET_KEY": ""}):
            self.assertFalse(self.worker(self.successful).run(120))
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.item()["last_error"], "credentials_missing")
        self.assertEqual(self.item()["status"], "pending")
        self.assertEqual((self.directory / f"{PAGE}.md").read_bytes(), before)
        self.assert_no_association()

    def test_retry_after_survives_restart(self):
        def limited(method, url, kwargs):
            return reply(url, 429, headers={"Retry-After": "120"})

        start = self.clock()
        self.assertFalse(self.worker(limited).run(60))
        self.assertEqual(len(self.session.calls), 1)
        self.assertGreaterEqual(self.item()["next_attempt_at"], start + 120)
        self.clock.sleep(121)
        self.assertTrue(self.worker(self.successful).run(120))
        self.assertEqual(self.item()["attempts"], 2)

    def test_pending_job_is_polled_after_restart_without_resubmission(self):
        def pending(method, url, kwargs):
            if method == "POST":
                return self.successful(method, url, kwargs)
            return reply(url, payload={"status": "pending"})

        self.assertFalse(self.worker(pending).run(25))
        self.assertEqual(self.item()["job_id"], "job-1")
        self.assertTrue(self.worker(self.successful).run(120))
        self.assertFalse(any(method == "POST" for method, _, _ in self.session.calls))

    def test_uncertain_post_is_reconciled_without_duplicate_submission(self):
        def lost_response(method, url, kwargs):
            raise requests.ReadTimeout("response lost after acceptance")

        self.assertFalse(self.worker(lost_response).run(30))
        self.assertEqual(self.item()["status"], "uncertain")
        self.clock.sleep(61)

        def reconcile(method, url, kwargs):
            self.assertEqual(method, "GET")
            if url == const.WAYBACK_API:
                return reply(
                    url,
                    payload={
                        "archived_snapshots": {
                            "closest": {
                                "available": True,
                                "status": "200",
                                "timestamp": self.stamp,
                                "url": f"https://web.archive.org/web/{self.stamp}/{self.official}",
                            }
                        }
                    },
                )
            return self.successful(method, url, kwargs)

        self.assertTrue(self.worker(reconcile).run(120))
        self.assertEqual(self.item()["attempts"], 1)

    def test_crash_after_submission_intent_does_not_resubmit(self):
        path = self.directory / "capture-queue.json"
        queue = read_json(path)
        item = next(iter(queue.values()))
        item.update(status="submitting", submitted_at=self.clock(), attempts=1)
        write_json(path, queue)

        def empty_archive(method, url, kwargs):
            self.assertEqual(method, "GET")
            return reply(url, payload={"archived_snapshots": {}})

        self.assertFalse(self.worker(empty_archive).run(60))
        self.assertEqual(self.item()["status"], "uncertain")
        self.assertEqual(len(self.session.calls), 1)

    def test_independent_capture_mismatches_are_not_associated(self):
        alternatives = [
            html(text="Changed independently."),
            html("eng"),
            html(page="11-young-women"),
        ]
        for raw in alternatives:
            with self.subTest(raw=raw):
                queue = read_json(self.directory / "capture-queue.json")
                item = next(iter(queue.values()))
                item.update(
                    status="verifying",
                    candidate_timestamp=self.stamp,
                    candidate_url=self.official,
                    next_attempt_at=0,
                )
                write_json(self.directory / "capture-queue.json", queue)
                self.assertFalse(
                    self.worker(lambda method, url, kwargs: reply(url, body=raw)).run(
                        120
                    )
                )
                self.assertEqual(self.item()["status"], "mismatch")
                self.assert_no_association()

    def test_wrong_language_redirect_and_replay_timestamp_are_not_associated(self):
        def wrong_url(method, url, kwargs):
            if "/save/status/" in url:
                return reply(
                    url,
                    payload={
                        "status": "success",
                        "timestamp": self.stamp,
                        "original_url": self.official.replace("por", "eng"),
                    },
                )
            return self.successful(method, url, kwargs)

        self.assertFalse(self.worker(wrong_url).run(120))
        self.assertEqual(self.item()["status"], "mismatch")
        self.assert_no_association()

    def test_old_pending_content_is_not_attached_to_refreshed_download(self):
        original_key = next(iter(read_json(self.directory / "capture-queue.json")))
        self.raw = html(text="New edition wording.")
        self.seed()
        self.assertEqual(len(read_json(self.directory / "capture-queue.json")), 2)
        self.assertFalse(self.worker(self.successful).run(60))
        self.assertEqual(
            read_json(self.directory / "capture-queue.json")[original_key]["status"],
            "superseded",
        )
        self.assertNotEqual(self.source()["wayback"]["queue_key"], original_key)
        self.assertEqual(
            self.source()["wayback"]["content_sha256"], self.source()["content_sha256"]
        )

    def test_accepted_old_job_is_verified_without_overwriting_new_text_capture(self):
        path = self.directory / "capture-queue.json"
        queue = read_json(path)
        old_key = next(iter(queue))
        old_hash = queue[old_key]["content_sha256"]
        queue[old_key].update(
            status="submitted", job_id="job-1", submitted_at=self.clock()
        )
        write_json(path, queue)
        original_html = self.raw
        self.raw = html(text="New wording while the capture is running.")
        self.seed()

        def original_capture(method, url, kwargs):
            if "/web/" in url:
                return reply(url, body=original_html)
            return self.successful(method, url, kwargs)

        worker = self.worker(original_capture)
        queue = read_json(path)
        worker.step(self.directory, old_key, queue)
        worker.step(self.directory, old_key, queue)
        self.assertEqual(read_json(path)[old_key]["status"], "verified")
        source = self.source()
        self.assertNotEqual(source["content_sha256"], old_hash)
        self.assertEqual(source["wayback"]["status"], "pending")
        self.assertEqual(source["previous_captures"][0]["content_sha256"], old_hash)

    def test_historical_source_is_not_submitted_to_save_page_now(self):
        directory = self.root / "por" / "2026-03"
        downloader = HandbookDownloader(directory, "por")
        url = f"https://web.archive.org/web/20260319090000id_/{self.official}"
        with patch.object(downloader, "find_link", return_value=(url, 0)), patch(
            "handbook.downloader.get_response", return_value=reply(url, body=self.raw)
        ), patch("handbook.downloader.sleep"):
            downloader.get_page("2026-03", PAGE)
        self.assertFalse((directory / "capture-queue.json").exists())
        source = read_json(directory / "sources.json")[PAGE]
        self.assertEqual(source["wayback"]["method"], "historical_source")
        self.assertEqual(source["wayback"]["timestamp"], "20260319090000")


if __name__ == "__main__":
    unittest.main()
