"""Run the real capture worker over HTTP against the local Postman simulation."""

import argparse
import datetime as dt
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

import requests

from handbook import const
from handbook.downloader import HandbookDownloader
from handbook.preservation import (
    CaptureWorker,
    content_hash,
    enqueue,
    read_json,
    write_json,
)


class SimulationTransport:
    """Map provider hosts to one local route; keep the caller's original response URL."""

    def __init__(self, base):
        self.base = base
        self.session = requests.Session()
        self.session.trust_env = False

    def request(self, method, url, **kwargs):
        parts = urlsplit(url)
        assert parts.hostname in ("web.archive.org", "archive.org")
        target = self.base + "/archive" + parts.path
        if parts.query:
            target += "?" + parts.query
        response = self.session.request(method, target, **kwargs)
        response.url = url
        return response


class Clock:
    def __init__(self):
        self.value = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc).timestamp()

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:4018")
    args = parser.parse_args()
    if urlsplit(args.url).hostname not in ("127.0.0.1", "localhost"):
        parser.error("Only a local simulation URL is supported")
    control = requests.Session()
    control.trust_env = False
    count = 0
    for language in const.LANGUAGES:
        for mode in (
            "success",
            "mismatch",
            "wrong-language",
            "rate-limit",
            "uncertain",
        ):
            control.post(
                args.url + "/archive/__reset", json={"mode": mode}, timeout=5
            ).raise_for_status()
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / language / "2026-10"
                directory.mkdir(parents=True)
                page = const.PAGES[0]
                raw = (
                    f'<article data-content-type="chapter" lang="{language}" data-uri="/manual/general-handbook/{page}">'
                    '<div class="body"><header><p>0.</p><h1>Introduction</h1></header>'
                    '<div class="body-block"><p>Official example text.</p></div></div></article>'
                ).encode()
                text = HandbookDownloader.render_chapter(raw, language, page)
                (directory / f"{page}.md").write_text(text)
                url = f"{const.PREFIX}{page}?lang={language}"
                source = {
                    "edition": "2026-10",
                    "language": language,
                    "official_url": url,
                    "retrieved_url": url,
                    "html_sha256": hashlib.sha256(raw).hexdigest(),
                    "content_sha256": content_hash(text),
                }
                enqueue(directory, page, source)
                write_json(directory / "sources.json", {page: source})
                clock = Clock()
                with patch.dict(
                    os.environ,
                    {
                        "IA_ACCESS_KEY": "simulation-access",
                        "IA_SECRET_KEY": "simulation-secret",
                    },
                ):

                    def worker():
                        return CaptureWorker(
                            root,
                            [directory],
                            clock=clock,
                            sleep=clock.sleep,
                            session=SimulationTransport(args.url),
                        )

                    if mode in ("rate-limit", "uncertain"):
                        assert not worker().run(60), mode
                        clock.sleep(121)
                    complete = worker().run(180)
                saved = read_json(directory / "sources.json")[page]["wayback"]
                stats = control.get(args.url + "/archive/__stats", timeout=5).json()
                if mode in ("mismatch", "wrong-language"):
                    assert not complete and saved["status"] == "mismatch", (
                        language,
                        mode,
                        saved,
                    )
                    assert "url" not in saved and "timestamp" not in saved
                else:
                    assert complete and saved["status"] == "verified", (
                        language,
                        mode,
                        saved,
                    )
                    assert saved["timestamp"] == "20261029120000"
                    assert saved["url"].endswith("lang=" + language)
                assert stats["posts"] == (2 if mode == "rate-limit" else 1), (
                    mode,
                    stats,
                )
                print(f"PASS {language}: {mode}")
                count += 1
    print(f"{count} HTTP simulation scenarios passed")


if __name__ == "__main__":
    main()
