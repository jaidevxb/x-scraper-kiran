"""Apify REST client: start an actor run, wait for it, page through the dataset."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.apify.com/v2"
TERMINAL_OK = {"SUCCEEDED"}
TERMINAL_BAD = {"FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT"}


class ApifyError(RuntimeError):
    """A recoverable Apify problem: the day stays pending and we retry next run."""


class ApifyFatalError(ApifyError):
    """Retrying will not help. Abort the whole run and tell a human."""


class ApifyCreditsExhausted(ApifyFatalError):
    """Out of credits or plan limit hit."""


class ApifyAuthError(ApifyFatalError):
    """Token wrong, revoked, or the actor id does not exist."""


class ApifyClient:
    def __init__(self, token: str, actor_id: str, retries: int = 4, timeout: int = 1800) -> None:
        self.token = token
        self.actor_id = actor_id
        self.retries = max(1, retries)
        self.run_timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "xscraper/1.0"})

    # --- plumbing --------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{API_ROOT}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Retry on transient failures with exponential backoff and jitter."""
        params = dict(kwargs.pop("params", {}) or {})
        params["token"] = self.token
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.request(
                    method, self._url(path), params=params, timeout=120, **kwargs
                )
            except requests.RequestException as exc:
                # No network at all: laptop woke up before Wi-Fi reconnected.
                last_error = exc
                log.warning("Network error on %s %s (attempt %d/%d): %s",
                            method, path, attempt, self.retries, exc)
            else:
                if resp.status_code in (402, 403):
                    body = resp.text[:400]
                    lowered = body.lower()
                    if any(word in lowered for word in
                           ("credit", "limit", "usage", "payment", "quota")):
                        raise ApifyCreditsExhausted(
                            f"Apify rejected the request ({resp.status_code}). This usually "
                            f"means the account is out of credits or on the free demo plan. "
                            f"Response: {body}"
                        )
                    raise ApifyError(f"Apify returned {resp.status_code}: {body}")

                if resp.status_code == 401:
                    raise ApifyAuthError(
                        "Apify returned 401 Unauthorized — APIFY_TOKEN is wrong or revoked. "
                        "Get a fresh one at https://console.apify.com/settings/integrations"
                    )
                if resp.status_code == 404:
                    raise ApifyAuthError(
                        f"Apify returned 404 for {path}. Check APIFY_ACTOR_ID "
                        f"(current: {self.actor_id})."
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = ApifyError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    log.warning("Apify %s on %s (attempt %d/%d), backing off",
                                resp.status_code, path, attempt, self.retries)
                else:
                    return resp

            if attempt < self.retries:
                delay = min(60.0, 2.0 ** attempt) + random.uniform(0, 1.5)
                time.sleep(delay)

        raise ApifyError(f"{method} {path} failed after {self.retries} attempts: {last_error}")

    # --- actor runs ------------------------------------------------------
    def start_run(self, actor_input: dict[str, Any]) -> dict[str, Any]:
        resp = self._request(
            "POST",
            f"acts/{self.actor_id}/runs",
            json=actor_input,
            headers={"Content-Type": "application/json"},
        )
        data = resp.json().get("data") or {}
        if not data.get("id"):
            raise ApifyError(f"Apify did not return a run id: {resp.text[:300]}")
        log.info("Started Apify run %s", data["id"])
        return data

    def wait_for_run(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.run_timeout
        poll = 5.0
        while True:
            resp = self._request("GET", f"actor-runs/{run_id}")
            data = resp.json().get("data") or {}
            status = data.get("status", "UNKNOWN")

            if status in TERMINAL_OK:
                log.info("Run %s succeeded", run_id)
                return data
            if status in TERMINAL_BAD:
                # A failed run can still have written partial results, so we
                # read the dataset anyway rather than throwing everything away.
                log.warning("Run %s ended as %s — will still read whatever it produced",
                            run_id, status)
                return data

            if time.monotonic() > deadline:
                self.abort_run(run_id)
                raise ApifyError(
                    f"Run {run_id} did not finish within {self.run_timeout}s (status {status}); "
                    "aborted it. The day stays pending and the next run retries."
                )

            time.sleep(poll)
            poll = min(30.0, poll * 1.3)

    def abort_run(self, run_id: str) -> None:
        try:
            self._request("POST", f"actor-runs/{run_id}/abort")
        except ApifyError:
            log.warning("Could not abort run %s", run_id, exc_info=True)

    def dataset_items(self, dataset_id: str, hard_cap: int) -> list[dict[str, Any]]:
        """Page through the dataset; stop at hard_cap so a runaway run can't flood us."""
        items: list[dict[str, Any]] = []
        offset = 0
        page_size = 500
        while len(items) < hard_cap:
            want = min(page_size, hard_cap - len(items))
            resp = self._request(
                "GET",
                f"datasets/{dataset_id}/items",
                params={"format": "json", "clean": "true", "offset": offset, "limit": want},
            )
            try:
                batch = resp.json()
            except ValueError as exc:
                raise ApifyError(f"Dataset {dataset_id} returned non-JSON") from exc
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            offset += len(batch)
            if len(batch) < want:
                break
        log.info("Fetched %d items from dataset %s", len(items), dataset_id)
        return items

    def run_and_collect(self, actor_input: dict[str, Any], hard_cap: int) -> list[dict[str, Any]]:
        run = self.start_run(actor_input)
        finished = self.wait_for_run(run["id"])
        dataset_id = finished.get("defaultDatasetId") or run.get("defaultDatasetId")
        if not dataset_id:
            raise ApifyError(f"Run {run['id']} has no dataset id")
        items = self.dataset_items(dataset_id, hard_cap)
        if not items and finished.get("status") not in TERMINAL_OK:
            raise ApifyError(
                f"Run {run['id']} ended as {finished.get('status')} with no results."
            )
        return items
