#!/usr/bin/env python3
"""Tests for LinkedIn page navigation.

Regression cover for the 2026-09-22 failure: LinkedIn streams its company and
profile documents over a long-lived response, so DOMContentLoaded lands 23-45s
after the content is usable. Waiting on that lifecycle event blew past
Playwright's 30s default and every company was wrongly recorded as having no
decision-makers.
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent))
import linkedin_prospector
from linkedin_prospector import goto_linkedin, find_people_at_company

SCRIPT = Path(__file__).parent / "linkedin_prospector.py"
PROFILE_LINK = 'a[href*="/in/"]'


class FakePage:
    """Mimics LinkedIn: DOMContentLoaded never lands, but content is there."""

    def __init__(self, selectors_present=(), commit_ok=True, people_entries=None):
        self.selectors_present = set(selectors_present)
        self.commit_ok = commit_ok
        self.people_entries = people_entries or []
        self.goto_calls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until))
        if wait_until in ("domcontentloaded", "load", "networkidle"):
            raise PlaywrightTimeout(f"Timeout {timeout or 30000}ms exceeded")
        if not self.commit_ok:
            raise PlaywrightTimeout("Timeout exceeded")

    def wait_for_selector(self, selector, timeout=None):
        if selector in self.selectors_present:
            return object()
        raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded waiting for {selector}")

    # --- stubs for the path after the page renders ---
    class _Mouse:
        def wheel(self, dx, dy):
            pass

    mouse = _Mouse()

    def eval_on_selector_all(self, selector, script):
        return self.people_entries

    def evaluate(self, script):
        return False  # no "show more results" button


def test_succeeds_when_domcontentloaded_never_fires():
    page = FakePage(selectors_present={'a[href*="/in/"]'})
    assert goto_linkedin(page, "https://www.linkedin.com/company/legit-bytes/people/",
                         'a[href*="/in/"]') is True
    waits = [w for _, w in page.goto_calls]
    assert "domcontentloaded" not in waits, f"must not wait on document lifecycle: {waits}"
    assert waits == ["commit"], waits


def test_returns_false_when_content_never_appears():
    page = FakePage(selectors_present=set())
    assert goto_linkedin(page, "https://www.linkedin.com/company/empty/people/",
                         'a[href*="/in/"]') is False


def test_no_selector_means_navigation_only():
    page = FakePage(selectors_present=set())
    assert goto_linkedin(page, "https://www.linkedin.com/in/someone") is True


def test_slow_streaming_pages_no_longer_use_domcontentloaded():
    """Guard: company/profile/activity navigations must not use the lifecycle wait."""
    src = SCRIPT.read_text()
    offenders = []
    for num, line in enumerate(src.splitlines(), 1):
        if "page.goto(" not in line or "domcontentloaded" not in line:
            continue
        if re.search(r"people_url|search_url|profile_url|activity_url", line):
            offenders.append(f"line {num}: {line.strip()}")
    assert not offenders, "slow-streaming pages still wait on DOMContentLoaded:\n" + "\n".join(offenders)


class ExplodingPage(FakePage):
    """Navigation itself fails (LinkedIn timeout, network drop)."""

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until))
        raise PlaywrightTimeout("Timeout 30000ms exceeded")


def test_failed_lookup_is_flagged_not_recorded_as_empty():
    """A failed lookup must be distinguishable from 'company has no decision-makers'.

    Otherwise the caller writes a permanent no_contact_found row and the company
    is never retried — which is what poisoned 10 companies on 2026-09-22.
    """
    company = {"name": "LegitBytes", "slug": "legit-bytes",
               "url": "https://www.linkedin.com/company/legit-bytes/"}
    config = {"target_roles": ["cto"],
              "delay_between_actions": {"min_seconds": 0, "max_seconds": 0}}
    people = find_people_at_company(ExplodingPage(), company, config, set())
    assert people == [], people
    assert company.get("lookup_error") is True, "failed lookup must set lookup_error"


def test_page_that_renders_but_has_nobody_is_not_flagged():
    """A rendered page with no usable people is a genuine empty — safe to record."""
    company = {"name": "Empty Co", "slug": "empty-co",
               "url": "https://www.linkedin.com/company/empty-co/"}
    config = {"target_roles": ["cto"],
              "delay_between_actions": {"min_seconds": 0, "max_seconds": 0}}
    page = FakePage(selectors_present={PROFILE_LINK}, people_entries=[])
    people = find_people_at_company(page, company, config, set())
    assert people == [], people
    assert not company.get("lookup_error"), "a rendered-but-empty company must not be flagged"


def test_page_that_never_renders_is_flagged_for_retry():
    """Throttling stalls the document; we never saw a people list, so retry later.

    Recording it as 'no contacts' would drop the lead permanently.
    """
    linkedin_prospector.THROTTLE_BACKOFF_SECONDS = 0  # don't sleep in tests
    company = {"name": "Stalled Co", "slug": "stalled-co",
               "url": "https://www.linkedin.com/company/stalled-co/"}
    config = {"target_roles": ["cto"],
              "delay_between_actions": {"min_seconds": 0, "max_seconds": 0}}
    people = find_people_at_company(FakePage(selectors_present=set()), company, config, set())
    assert people == [], people
    assert company.get("lookup_error") is True, "a page that never rendered must be retried, not recorded"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'OK'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
