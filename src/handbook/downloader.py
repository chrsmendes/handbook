from bs4 import BeautifulSoup
import requests
from markdownify import MarkdownConverter
from markdownify import ATX, UNDERSCORE
import re
import datetime
import hashlib
import json
from . import const
from time import sleep
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .preservation import (
    content_hash,
    edition_lock,
    enqueue,
    now_iso,
    read_json,
    write_json,
)


class ArchiveUnavailable(RuntimeError):
    """The archive cannot be queried; this does not mean a capture is missing."""


def get_response(url, **kwargs):
    """Retry transient connection failures without changing the requested source."""
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=const.REQUEST_TIMEOUT, **kwargs)
            if response.status_code in (500, 502, 503, 504) and attempt < 2:
                print(
                    f"----Server error {response.status_code}; retrying ({attempt + 1}/2)",
                    flush=True,
                )
                sleep(attempt + 1)
                continue
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            print(f"----Connection failed; retrying ({attempt + 1}/2)", flush=True)
            sleep(attempt + 1)


class HandbookConverter(MarkdownConverter):
    def convert_td(self, el, text, convert_as_inline):
        """Preserve lists inside tables using HTML breaks."""
        colspan = 1
        if "colspan" in el.attrs:
            colspan = int(el["colspan"])

        text = text.strip().replace("\n", "<br/>")  # replace newlines with breaks
        text = re.sub(r"(?<!\\)\*", "•", text)  # replace asterisks with bullet points

        return " " + text + " |" * colspan

    def convert_img(self, el, text, convert_as_inline):
        """Omit images from Markdown output."""
        return ""

    def convert_a(self, el, text, convert_as_inline):
        """Keep adaptation/optional references for language-aware link sanitizing."""
        if not text and el.get("href", "").startswith(
            "/study/manual/general-handbook/0-introductory-overview"
        ):
            return f"[[AO]]({el['href']})"

        return super().convert_a(el, text, convert_as_inline)


class HandbookDownloader:
    def __init__(self, dir: Path, language=const.DEFAULT_LANGUAGE):
        if language not in const.LANGUAGES:
            raise ValueError(f"Unsupported language: {language}")
        self.language = language
        self.dir = dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sources_file = self.dir / "sources.json"
        self.sources = (
            json.loads(self.sources_file.read_text(encoding="utf8"))
            if self.sources_file.exists()
            else {}
        )

    @staticmethod
    def convert(soup):
        # Remove extra header in body of tables
        for row in soup.find_all("tbody"):
            for extra in row.find_all("p", {"class": "label"}):
                extra.decompose()

        # Remove footnotes
        # To my knowledge, the only page with footnotes is 30.8
        for a in soup.find_all("a", {"class": "note-ref"}):
            a.decompose()

        # Remove images
        for img in soup.select('div[class*="imageWrapper-"]'):
            img.decompose()
        for img in soup.select('span[class*="imageWrapper-"]'):
            img.decompose()

        text = (
            HandbookConverter(heading_style=ATX, strong_em_symbol=UNDERSCORE)
            .convert_soup(soup)
            .strip()
        )
        text = re.sub(r"\n\s*\n", "\n\n", text)  # remove extra newlines

        text = re.sub(
            r"^ +([\w#])", r"\1", text, flags=re.MULTILINE
        )  # remove leading spaces

        # Some of the headers are missing header titles ##
        def add_title(match):
            num = len(match.group(1).split("."))
            return f"{match.group(1)}\n\n{'#' * num} {match.group(3)}"

        text = re.sub(
            r"^(\d{1,2}(\.?\d{0,2}){0,3})\n\n([^#]+?)",
            add_title,
            text,
            flags=re.MULTILINE,
        )

        # merge number and header
        text = re.sub(
            r"(\d{1,2}(\.?\d{0,2}){0,3})\n\n(#+) (.*)",
            r"\3 \1 \4",
            text,
        )

        # Some of the headers don't start on a new line
        text = re.sub(r"([^\n#])(#+ (\d{1,2}(\.?\d{0,2}){0,3}))", r"\1\n\n\2", text)

        return text

    def get_page(self, date, page, refresh=False):
        """Download official content in the requested language, without translating."""
        filename = self.dir / f"{page}.md"

        if filename.exists() and not refresh:
            source = self.sources.get(page, {})
            if self.language != "eng" and source.get("language") != self.language:
                raise ValueError(
                    f"File has no verified {self.language} source: {filename}"
                )
            print(f"{filename} already exists, skipping")
            return True

        print(f"{filename}")

        url, delay = self.find_link(date, page)
        if url is None or delay is None:
            return False
        try:
            response = get_response(url)
        except requests.RequestException as error:
            if urlsplit(url).hostname == "web.archive.org":
                raise ArchiveUnavailable(
                    f"Archive download unavailable: {error}"
                ) from error
            raise
        finally:
            sleep(delay)
        self.validate_url(response.url, self.language)
        if urlsplit(url).hostname == "web.archive.org":
            requested_stamp = urlsplit(url).path.split("/")[2][:14]
            returned_path = urlsplit(response.url).path
            if urlsplit(
                response.url
            ).hostname != "web.archive.org" or not returned_path.startswith(
                f"/web/{requested_stamp}"
            ):
                raise ValueError(
                    "Archive redirected to a different capture; existing file preserved"
                )
        final = self.render_chapter(response.content, self.language, page)
        source = {
            "edition": date,
            "language": self.language,
            "official_url": f"{const.PREFIX}{page}?lang={self.language}",
            "retrieved_url": response.url,
            "retrieved_at": now_iso(),
            "html_sha256": hashlib.sha256(response.content).hexdigest(),
            "content_sha256": content_hash(final),
        }
        with edition_lock(self.dir):
            self.sources = read_json(self.sources_file)
            previous = self.sources.get(page, {})
            if previous.get("previous_captures"):
                source["previous_captures"] = previous["previous_captures"]
            if (
                previous.get("wayback", {}).get("status") == "verified"
                and previous.get("content_sha256") != source["content_sha256"]
            ):
                source.setdefault("previous_captures", []).append(previous["wayback"])
            if previous.get("content_sha256") == source["content_sha256"]:
                if previous.get("wayback", {}).get("status") == "verified":
                    source["wayback"] = previous["wayback"]
            temporary = filename.with_suffix(".md.tmp")
            temporary.write_text(final, encoding="utf8")
            temporary.replace(filename)
            if urlsplit(url).hostname == "web.archive.org":
                stamp = urlsplit(url).path.split("/")[2][:14]
                source["wayback"] = {
                    "status": "verified",
                    "timestamp": stamp,
                    "url": f"https://web.archive.org/web/{stamp}/{source['official_url']}",
                    "content_sha256": source["content_sha256"],
                    "verified_at": now_iso(),
                    "method": "historical_source",
                }
            else:
                enqueue(self.dir, page, source)
            self.sources[page] = source
            write_json(self.sources_file, self.sources)
        print("----Saved")
        return True

    @classmethod
    def render_chapter(cls, content, language, page):
        """Use the same strict validation and conversion for live and archived HTML."""
        soup = BeautifulSoup(content, "html.parser")
        article = soup.find("article", attrs={"data-content-type": "chapter"})
        if article is None or article.get("lang") != language:
            raise ValueError(
                f"Chapter content is not in the requested language: {language}"
            )
        if article.get("data-uri") != f"/manual/general-handbook/{page}":
            raise ValueError("The source returned a different chapter")
        body = article.find("div", {"class": "body"})
        if body is None:
            raise ValueError("Chapter body not found")
        header = body.find("header")
        content = body.find("div", {"class": "body-block"})
        if header is None or content is None or not content.get_text(strip=True):
            raise ValueError("Chapter title or text missing; existing file preserved")
        final = cls.convert(header) + "\n\n" + cls.convert(content)
        return final + ("\n" if language != "eng" else "")

    @staticmethod
    def validate_url(url, language=const.DEFAULT_LANGUAGE):
        """Reject redirects to a different language or source."""
        parts = urlsplit(url)
        if parts.hostname == "web.archive.org":
            match = re.fullmatch(r"/web/\d{14}(?:id_)?/(https?://.+)", parts.path)
            if not match:
                raise ValueError("Invalid archive URL")
            parts = urlsplit(match.group(1) + "?" + parts.query)
        if (
            parts.hostname != "www.churchofjesuschrist.org"
            or not parts.path.startswith("/study/manual/general-handbook/")
            or parse_qs(parts.query).get("lang") != [language]
        ):
            raise ValueError(
                "The source URL does not match the official site and requested language"
            )

    def find_link(self, curr_date, page):
        url = f"{const.PREFIX}{page}?lang={self.language}"

        if curr_date == const.DATES[-1]:
            print("----Current version from the official Church website")
            return url, 0.1

        # Reference dates delimit archive searches; they do not prove a Portuguese edition.
        index = const.DATES.index(curr_date)
        start = datetime.datetime.strptime(curr_date, "%Y-%m")
        if index + 1 < len(const.DATES):
            end = datetime.datetime.strptime(const.DATES[index + 1], "%Y-%m")
        else:
            end = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        guess = min(end, datetime.datetime.now()) - datetime.timedelta(seconds=1)
        while guess >= start:
            try:
                response = get_response(
                    const.WAYBACK_API,
                    params={"url": url, "timestamp": guess.strftime("%Y%m%d%H%M%S")},
                )
                closest = (
                    response.json().get("archived_snapshots", {}).get("closest", {})
                )
            except requests.RequestException as error:
                raise ArchiveUnavailable(
                    f"Archive lookup unavailable: {error}"
                ) from error
            finally:
                sleep(const.WAYBACK_DELAY)
            if closest.get("available") and str(closest.get("status")) == "200":
                stamp = closest["timestamp"]
                captured = datetime.datetime.strptime(stamp, "%Y%m%d%H%M%S")
                self.validate_url(closest["url"], self.language)
                if start <= captured < end:
                    print(f"----Found snapshot {captured:%Y-%m-%d}")
                    return (
                        f"https://web.archive.org/web/{stamp}id_/{url}",
                        const.WAYBACK_DELAY,
                    )
            guess = guess.replace(
                day=1, hour=0, minute=0, second=0
            ) - datetime.timedelta(seconds=1)

        print("----No snapshot in the requested language and date range, skipping")
        return None, None
