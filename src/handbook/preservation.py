"""Durable Save Page Now queue. Credentials are only read from the environment."""

from contextlib import contextmanager
import datetime as dt
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urlsplit

import requests

SPN = "https://web.archive.org/save"
TERMINAL = {"verified", "mismatch", "superseded", "rejected"}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def content_hash(markdown):
    """Compare chapter wording/structure independently of rewritten link targets."""
    text = re.sub(r"\[(.*?)\]\((.*?)\)", lambda m: f"[{m[1].strip()}]", markdown)
    text = unicodedata.normalize("NFC", text)
    return hashlib.sha256(" ".join(text.split()).encode("utf8")).hexdigest()


def read_json(path, default=None):
    return (
        json.loads(path.read_text(encoding="utf8"))
        if path.exists()
        else (default or {})
    )


def write_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    temporary.replace(path)


@contextmanager
def edition_lock(directory):
    with (directory / ".capture.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def enqueue(directory, page, source):
    """Caller holds edition_lock. Historic replays are never sent to SPN."""
    if (
        source.get("wayback", {}).get("status") == "verified"
        and source["wayback"].get("content_sha256") == source["content_sha256"]
    ):
        return source["wayback"].get("queue_key")
    key = hashlib.sha256(
        f"{source['language']}:{source['edition']}:{page}:{source['content_sha256']}".encode()
    ).hexdigest()
    path = directory / "capture-queue.json"
    queue = read_json(path)
    if key not in queue:
        queue[key] = {
            "edition": source["edition"],
            "language": source["language"],
            "page": page,
            "url": source["official_url"],
            "content_sha256": source["content_sha256"],
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": 0,
            "created_at": now_iso(),
        }
        write_json(path, queue)
    source["wayback"] = {"status": queue[key]["status"], "queue_key": key}
    if queue[key]["status"] == "verified":
        source["wayback"].update(queue[key]["capture"])
    return key


def seed_queue(directory):
    """Resume validated downloads made before queue support, not unverified legacy files."""
    sources_path = directory / "sources.json"
    if not sources_path.exists():
        return
    with edition_lock(directory):
        sources = read_json(sources_path)
        for page, source in sources.items():
            file = directory / f"{page}.md"
            if not file.exists() or not source.get("html_sha256"):
                continue
            if (
                urlsplit(source.get("retrieved_url", "")).hostname
                != "www.churchofjesuschrist.org"
            ):
                continue
            if source.get("edition", directory.name) != directory.name:
                continue
            digest = content_hash(file.read_text(encoding="utf8"))
            if source.get("content_sha256", digest) != digest:
                continue
            source.update(edition=directory.name, content_sha256=digest)
            enqueue(directory, page, source)
        write_json(sources_path, sources)


class CaptureWorker:
    def __init__(
        self,
        root,
        directories,
        *,
        interval=10,
        poll_interval=10,
        clock=time.time,
        sleep=time.sleep,
        session=None,
    ):
        self.root = Path(root)
        self.directories = list(directories)
        self.interval = max(10, interval)
        self.poll_interval = max(5, poll_interval)
        self.clock, self.sleep = clock, sleep
        self.session = session or requests.Session()
        self.access = os.environ.get("IA_ACCESS_KEY", "")
        self.secret = os.environ.get("IA_SECRET_KEY", "")
        self.state_file = self.root / ".wayback-state.json"
        self.state = read_json(self.state_file)

    def auth_headers(self):
        return {
            "Accept": "application/json",
            "Authorization": f"LOW {self.access}:{self.secret}",
        }

    def defer(self, item, reason, response=None, minimum=60):
        delay = min(3600, max(minimum, 60 * 2 ** min(item.get("failures", 0), 6)))
        if response is not None:
            retry = response.headers.get("Retry-After", "")
            try:
                delay = max(delay, float(retry))
            except (TypeError, ValueError):
                try:
                    delay = max(
                        delay, parsedate_to_datetime(retry).timestamp() - self.clock()
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        item["failures"] = item.get("failures", 0) + 1
        item["last_error"] = reason
        item["next_attempt_at"] = self.clock() + delay
        self.state["not_before"] = item["next_attempt_at"]
        write_json(self.state_file, self.state)

    def http(self, method, url, *, authenticated=False, **kwargs):
        headers = (
            self.auth_headers() if authenticated else {"Accept": "application/json"}
        )
        # Never forward credentials across redirects, including replay downloads.
        return self.session.request(
            method, url, headers=headers, timeout=30, allow_redirects=False, **kwargs
        )

    def verify(self, item):
        from .downloader import HandbookDownloader

        stamp, original = item["candidate_timestamp"], item["candidate_url"]
        if not re.fullmatch(r"\d{14}", stamp):
            raise ValueError("invalid_capture_timestamp")
        dt.datetime.strptime(stamp, "%Y%m%d%H%M%S")
        HandbookDownloader.validate_url(original, item["language"])
        if urlsplit(original).path != urlsplit(item["url"]).path:
            raise ValueError("different_chapter_url")
        url = f"https://web.archive.org/web/{stamp}id_/{original}"
        result = self.http("GET", url)
        if result.status_code != 200:
            self.defer(item, f"replay_http_{result.status_code}", result)
            return
        if result.url != url:
            raise ValueError("different_capture_replay")
        text = HandbookDownloader.render_chapter(
            result.content, item["language"], item["page"]
        )
        if content_hash(text) != item["content_sha256"]:
            raise ValueError("capture_content_mismatch")
        item["status"] = "verified"
        item["capture"] = {
            "url": f"https://web.archive.org/web/{stamp}/{original}",
            "timestamp": stamp,
            "content_sha256": item["content_sha256"],
            "verified_at": now_iso(),
        }
        item.pop("last_error", None)
        item["next_attempt_at"] = 0

    def reconcile(self, item):
        from . import const

        result = self.http(
            "GET",
            const.WAYBACK_API,
            params={
                "url": item["url"],
                "timestamp": dt.datetime.fromtimestamp(
                    item["submitted_at"], dt.timezone.utc
                ).strftime("%Y%m%d%H%M%S"),
            },
        )
        if result.status_code != 200:
            self.defer(item, f"reconcile_http_{result.status_code}", result)
            return
        candidate = result.json().get("archived_snapshots", {}).get("closest", {})
        if candidate.get("available") and str(candidate.get("status")) == "200":
            stamp = candidate.get("timestamp", "")
            if re.fullmatch(r"\d{14}", stamp):
                captured = (
                    dt.datetime.strptime(stamp, "%Y%m%d%H%M%S")
                    .replace(tzinfo=dt.timezone.utc)
                    .timestamp()
                )
                # SPN may reuse a capture from its documented one-hour deduplication window.
                if captured >= int(item["submitted_at"]) - 3600:
                    # Verify the original URL returned by availability, not an assumed URL.
                    from .downloader import HandbookDownloader

                    HandbookDownloader.validate_url(candidate["url"], item["language"])
                    match = re.fullmatch(
                        r"/web/\d{14}(?:id_)?/(https?://.+)",
                        urlsplit(candidate["url"]).path,
                    )
                    original = match[1] + "?" + urlsplit(candidate["url"]).query
                    item.update(
                        status="verifying",
                        candidate_timestamp=stamp,
                        candidate_url=original,
                    )
                    return
        # SPN has no idempotency key. An uncertain POST is never blindly submitted again.
        self.defer(item, "submission_uncertain_no_matching_capture_yet", minimum=3600)

    def step(self, directory, key, queue):
        item = queue[key]
        sources_path = directory / "sources.json"
        sources = read_json(sources_path)
        source = sources.get(item["page"], {})
        file = directory / f"{item['page']}.md"
        current_matches = not (
            source.get("content_sha256") != item["content_sha256"]
            or not file.exists()
            or content_hash(file.read_text(encoding="utf8")) != item["content_sha256"]
        )
        if (
            not current_matches
            and item["status"] == "pending"
            and not item.get("job_id")
        ):
            item.update(status="superseded", last_error="download_changed")
            write_json(directory / "capture-queue.json", queue)
            return
        try:
            if item["status"] == "submitting":
                item["status"] = "uncertain"  # process died after persisting the intent
            if item["status"] == "uncertain":
                self.reconcile(item)
            elif item["status"] == "verifying":
                self.verify(item)
            elif item.get("job_id"):
                result = self.http(
                    "GET", f"{SPN}/status/{item['job_id']}", authenticated=True
                )
                if result.status_code == 404:
                    item.update(status="uncertain")  # SPN status has limited retention
                elif result.status_code != 200:
                    self.defer(item, f"status_http_{result.status_code}", result)
                else:
                    status = result.json()
                    if status.get("status") == "success":
                        item.update(
                            status="verifying",
                            candidate_timestamp=status["timestamp"],
                            candidate_url=status["original_url"],
                        )
                    elif status.get("status") == "pending":
                        item["next_attempt_at"] = self.clock() + self.poll_interval
                    elif status.get("status") == "error":
                        item.setdefault("failed_jobs", []).append(item.pop("job_id"))
                        item["status"] = "pending"
                        self.defer(item, "spn_job_failed")
                    else:
                        self.defer(item, "invalid_status_response")
            else:
                from .downloader import HandbookDownloader

                HandbookDownloader.validate_url(item["url"], item["language"])
                if urlsplit(item["url"]).hostname != "www.churchofjesuschrist.org":
                    raise ValueError("save_requires_official_live_url")
                item.update(
                    status="submitting",
                    submitted_at=self.clock(),
                    attempts=item["attempts"] + 1,
                )
                self.state["last_submission"] = self.clock()
                write_json(self.state_file, self.state)
                write_json(
                    directory / "capture-queue.json", queue
                )  # durable before POST
                result = self.http(
                    "POST",
                    SPN,
                    authenticated=True,
                    data={
                        "url": item["url"],
                        "capture_all": "0",
                        "capture_outlinks": "0",
                        "skip_first_archive": "1",
                        "if_not_archived_within": "3600",
                    },
                )
                if result.status_code in (401, 403, 429):
                    item["status"] = "pending"
                    self.defer(
                        item,
                        f"submit_http_{result.status_code}",
                        result,
                        minimum=3600 if result.status_code in (401, 403) else 60,
                    )
                elif result.status_code in (400, 405):
                    item.update(
                        status="rejected",
                        last_error=f"submit_http_{result.status_code}",
                    )
                elif result.status_code != 200:
                    item["status"] = "uncertain"
                    self.defer(item, f"submit_http_{result.status_code}", result)
                else:
                    body = result.json()
                    job = body.get("job_id")
                    if job and re.fullmatch(r"[A-Za-z0-9_-]+", str(job)):
                        item.update(
                            status="submitted",
                            job_id=job,
                            next_attempt_at=self.clock() + self.poll_interval,
                        )
                    elif body.get("timestamp") and body.get("original_url"):
                        item.update(
                            status="verifying",
                            candidate_timestamp=body["timestamp"],
                            candidate_url=body["original_url"],
                        )
                    else:
                        item["status"] = "uncertain"
                        self.defer(item, "submission_response_without_job_id")
        except requests.RequestException:
            if item["status"] == "submitting":
                item["status"] = "uncertain"
            self.defer(item, "network_error")
        except (ValueError, KeyError, TypeError):
            if item["status"] == "verifying":
                item.update(status="mismatch", last_error="capture_validation_failed")
            else:
                if item["status"] == "submitting":
                    item["status"] = "uncertain"
                self.defer(item, "invalid_api_response")
        if current_matches:
            source["wayback"] = {"status": item["status"], "queue_key": key}
            if item["status"] == "verified":
                source["wayback"].update(item["capture"])
        elif item["status"] == "verified":
            # A refresh must not erase an accepted job for the previous wording.
            previous = source.setdefault("previous_captures", [])
            if item["capture"] not in previous:
                previous.append(item["capture"])
        # A mismatched candidate stays only in the queue, never in sources.json.
        sources[item["page"]] = source
        write_json(sources_path, sources)
        write_json(directory / "capture-queue.json", queue)

    def run(self, max_seconds=300):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".wayback.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Another capture worker is active; leaving the queue intact.")
                return False
            # Refresh after the process lock; other invocations may have updated limits.
            self.state = read_json(self.state_file)
            items = [
                item
                for directory in self.directories
                for item in read_json(directory / "capture-queue.json").values()
            ]
            if not any(item["status"] not in TERMINAL for item in items):
                return all(item["status"] == "verified" for item in items)
            if not self.access or not self.secret:
                for directory in self.directories:
                    with edition_lock(directory):
                        path = directory / "capture-queue.json"
                        queue = read_json(path)
                        for item in queue.values():
                            if item["status"] not in TERMINAL:
                                item["last_error"] = "credentials_missing"
                        if queue:
                            write_json(path, queue)
                print(
                    "Wayback captures remain pending: configure IA_ACCESS_KEY and IA_SECRET_KEY."
                )
                return False
            deadline = self.clock() + max_seconds
            while self.clock() < deadline:
                tasks = []
                for directory in self.directories:
                    for key, item in read_json(
                        directory / "capture-queue.json"
                    ).items():
                        if item["status"] in TERMINAL:
                            continue
                        due = max(
                            item.get("next_attempt_at", 0),
                            self.state.get("not_before", 0),
                            self.state.get("last_request", 0) + 4.1,
                        )
                        if item["status"] == "pending" and not item.get("job_id"):
                            due = max(
                                due,
                                self.state.get("last_submission", 0) + self.interval,
                            )
                        tasks.append((due, str(directory), key))
                if not tasks:
                    return all(
                        item["status"] == "verified"
                        for directory in self.directories
                        for item in read_json(directory / "capture-queue.json").values()
                    )
                due, path, key = min(tasks)
                if due >= deadline:
                    break
                if due > self.clock():
                    self.sleep(min(1, due - self.clock()))
                    continue
                directory = Path(path)
                with edition_lock(directory):
                    queue = read_json(directory / "capture-queue.json")
                    if queue[key]["status"] not in TERMINAL:
                        self.state["last_request"] = self.clock()
                        write_json(self.state_file, self.state)
                        self.step(directory, key, queue)
            print(
                "Capture processing paused; unfinished jobs are saved for the next run."
            )
            return False
