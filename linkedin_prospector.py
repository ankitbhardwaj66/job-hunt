#!/usr/bin/env python3
"""
LinkedIn Prospector - Find small tech companies and active decision-makers.

Usage:
    First run:  python linkedin_prospector.py --login
                (Opens browser for manual LinkedIn login, saves session)

    Search:     python linkedin_prospector.py
                (Uses saved session to find companies and people)

    Both:       python linkedin_prospector.py --login --search
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SESSION_DIR = SCRIPT_DIR / ".linkedin_session"
DEBUG_DIR = SCRIPT_DIR / "debug"
PROSPECTS_SEEN_FILE = SCRIPT_DIR / ".seen_profiles.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def human_delay(min_sec, max_sec):
    """Sleep for a random duration to mimic human behavior."""
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)


def action_delay(config):
    """Short delay between actions (clicks, scrolls)."""
    d = config["delay_between_actions"]
    human_delay(d["min_seconds"], d["max_seconds"])


def page_delay(config):
    """Longer delay between page navigations."""
    d = config["delay_between_pages"]
    human_delay(d["min_seconds"], d["max_seconds"])


def random_scroll(page):
    """Scroll down randomly to simulate reading."""
    scroll_amount = random.randint(300, 800)
    page.mouse.wheel(0, scroll_amount)
    time.sleep(random.uniform(0.5, 1.5))


def load_seen_profiles():
    """Load previously seen profile URLs to avoid duplicates."""
    if PROSPECTS_SEEN_FILE.exists():
        with open(PROSPECTS_SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen_profiles(seen):
    with open(PROSPECTS_SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def do_login(playwright):
    """Open browser for manual login and save session."""
    print("\n--- LinkedIn Login ---")
    print("A browser will open. Please log in to LinkedIn manually.")
    print("After you're logged in and see your feed, come back here and press Enter.\n")

    browser = playwright.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()

    # Remove webdriver flag
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    page.goto("https://www.linkedin.com/login")
    input("\nPress Enter after you've logged in successfully...")

    # Verify login by checking for feed
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        time.sleep(3)
        if "feed" in page.url:
            print("Login verified! Saving session...")
            context.storage_state(path=str(SESSION_DIR / "state.json"))
            print(f"Session saved to {SESSION_DIR / 'state.json'}")
        else:
            print("Could not verify login. Please try again.")
    except Exception as e:
        print(f"Error verifying login: {e}")

    browser.close()


def debug_snapshot(page, name):
    """Save screenshot and HTML dump for debugging."""
    DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    page.screenshot(path=str(DEBUG_DIR / f"{name}_{ts}.png"), full_page=True)
    html = page.content()
    with open(DEBUG_DIR / f"{name}_{ts}.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [debug] Saved snapshot: debug/{name}_{ts}.png")


def extract_companies_from_page(page):
    """Extract company info from current search results page using multiple strategies."""
    found = []

    # Strategy 1: Find all links containing /company/ and extract from them
    all_links = page.eval_on_selector_all(
        'a[href*="/company/"]',
        """els => els.map(el => ({
            href: el.href,
            text: el.innerText.trim(),
            parentText: el.closest('li') ? el.closest('li').innerText.trim() : ''
        }))"""
    )

    seen_slugs = set()
    for link_info in all_links:
        href = link_info.get("href", "")
        match = re.search(r'/company/([^/?]+)', href)
        if not match:
            continue
        slug = match.group(1)

        # Skip navigation/generic links
        if slug in seen_slugs or slug in ("company", "companies"):
            continue
        # Skip links that are sub-pages like /company/foo/life
        after_slug = href.split(f"/company/{slug}")[-1].strip("/").split("?")[0]
        if after_slug and after_slug not in ("", "about"):
            continue

        seen_slugs.add(slug)

        # Get company name from link text or parent
        text = link_info.get("text", "").split("\n")[0].strip()
        if not text or len(text) > 100:
            text = slug.replace("-", " ").title()

        found.append({"name": text, "slug": slug})

    return found


def search_companies(page, config):
    """Search LinkedIn for small tech companies and return company info."""
    companies = []
    seen_companies = set()
    max_companies = config["max_companies_per_run"]
    keywords = config["search_keywords"]

    random.shuffle(keywords)

    for keyword in keywords:
        if len(companies) >= max_companies:
            break

        print(f"\nSearching for: {keyword}")

        for size_code in ["C"]:  # C = 11-50 employees
            if len(companies) >= max_companies:
                break

            url = f"https://www.linkedin.com/search/results/companies/?keywords={quote(keyword)}&companySize=%5B%22{size_code}%22%5D"

            try:
                page.goto(url, wait_until="domcontentloaded")
                # Wait for search results to render
                time.sleep(3)
                # Scroll to trigger lazy loading
                random_scroll(page)
                time.sleep(2)
                random_scroll(page)
                time.sleep(1)

                # Try to wait for result list items
                try:
                    page.wait_for_selector('a[href*="/company/"]', timeout=8000)
                except PlaywrightTimeout:
                    print(f"  No company links found for '{keyword}' (size {size_code})")
                    debug_snapshot(page, f"no_results_{keyword.replace(' ', '_')}_{size_code}")
                    continue

                page_companies = extract_companies_from_page(page)
                print(f"  Extracted {len(page_companies)} companies from page")

                # Build skip words from all search keywords + generic filler words
                skip_words = set()
                for kw in keywords:
                    skip_words.add(kw.lower())
                    for word in kw.lower().split():
                        skip_words.add(word)
                # Generic words that don't make a company name unique
                filler_words = {
                    # Connectors
                    "and", "the", "of", "a", "an", "in", "for", "by", "page",
                    "first", "best", "top", "leading", "new", "next", "gen",
                    # Business suffixes
                    "inc", "ltd", "llc", "pvt", "co", "corp", "group", "global",
                    "solutions", "services", "enterprise", "initiative", "leaders",
                    "markets", "search", "confidential", "stealth",
                    # Org types
                    "school", "academy", "lab", "labs", "hub", "network",
                    "community", "club", "institute", "center", "centre",
                    "factory", "incubator", "accelerator", "collective",
                    "platform", "digital", "online", "pro", "plus",
                    "studio", "forum", "mixer", "show", "expo",
                    "exchange", "program", "programme", "free", "code",
                    # Countries
                    "india", "usa", "uk", "us", "china", "japan", "korea",
                    "vietnam", "singapore", "indonesia", "malaysia", "thailand",
                    "philippines", "pakistan", "bangladesh", "nepal", "sri lanka",
                    "germany", "france", "spain", "italy", "netherlands", "sweden",
                    "norway", "denmark", "finland", "poland", "switzerland",
                    "australia", "canada", "brazil", "mexico", "argentina",
                    "nigeria", "kenya", "south africa", "egypt", "uae", "dubai",
                    "israel", "turkey", "russia", "ukraine", "ireland",
                    "europe", "asia", "africa", "mena", "latam", "apac",
                    # Major cities
                    "london", "berlin", "paris", "mumbai", "delhi", "bangalore",
                    "bengaluru", "hyderabad", "chennai", "pune", "kolkata",
                    "new york", "san francisco", "seattle", "austin", "boston",
                    "toronto", "sydney", "melbourne", "tokyo", "seoul",
                    "shanghai", "beijing", "hong kong", "hanoi", "jakarta",
                    "dubai", "tel aviv", "amsterdam", "stockholm", "lisbon",
                }
                # Also match multi-word locations as single tokens
                _location_phrases = {
                    "sri lanka", "south africa", "new york", "san francisco",
                    "hong kong", "tel aviv", "new zealand",
                }

                for comp in page_companies:
                    if len(companies) >= max_companies:
                        break
                    if comp["slug"] in seen_companies:
                        continue

                    orig_name = comp["name"].strip()
                    name_lower = orig_name.lower()

                    # Skip 1: "Page by X" — these are LinkedIn pages, not company listings
                    if name_lower.startswith("page by"):
                        print(f"    [skip] {orig_name} — LinkedIn page, not a company")
                        continue

                    # Skip 2: Names containing "stealth mode"
                    if "stealth mode" in name_lower:
                        print(f"    [skip] {orig_name} — stealth mode company")
                        continue

                    # Skip 3: VC / investor firms — they don't need dev help
                    vc_words = {"venture capital", "ventures", "capital", "investment",
                                "angel", "fund", "vc "}
                    if any(v in name_lower for v in vc_words):
                        print(f"    [skip] {orig_name} — VC/investment firm")
                        continue

                    # Skip 4: Check full name for disqualifying org types
                    disqualifying_types = {
                        "incubator", "accelerator", "academy", "school",
                        "bootcamp", "boot camp", "university", "college",
                        "institute", "forum", "mixer", "show", "expo",
                        "conference", "summit", "meetup", "podcast",
                    }
                    if any(dt in name_lower for dt in disqualifying_types):
                        print(f"    [skip] {orig_name} — org type not a target company")
                        continue

                    # Skip 5: Generic/keyword-stuffed names
                    check_name = name_lower
                    for phrase in _location_phrases:
                        check_name = check_name.replace(phrase, " ")
                    # Remove taglines after " - " or " | "
                    check_name = re.split(r'\s*[-|]\s*', check_name)[0].strip()
                    name_words = [w for w in check_name.replace(".", " ").split() if w]
                    meaningful_words = [w for w in name_words if w not in filler_words]
                    if meaningful_words and all(w in skip_words for w in meaningful_words):
                        print(f"    [skip] {orig_name} — generic/keyword name")
                        continue

                    # Skip 5: Community/event/edu names
                    community_words = {
                        "circle", "connect", "meetup", "event", "events",
                        "summit", "conference", "podcast", "media",
                        "magazine", "journal", "newsletter", "blog",
                        "forum", "mixer", "show", "expo", "fest",
                        "studio", "exchange", "program", "programme",
                        "free", "code", "deep", "blue", "make", "it",
                        "world", "worlds", "world's", "largest",
                        "team", "at",
                    }
                    remaining_after_keywords = [w for w in meaningful_words if w not in skip_words]
                    if not remaining_after_keywords:
                        pass  # already caught above
                    elif all(w in community_words for w in remaining_after_keywords):
                        print(f"    [skip] {orig_name} — community/event/edu, not a company")
                        continue

                    seen_companies.add(comp["slug"])

                    companies.append({
                        "name": comp["name"],
                        "slug": comp["slug"],
                        "url": f"https://www.linkedin.com/company/{comp['slug']}/",
                        "keyword": keyword,
                    })
                    print(f"    + {comp['name']}")

                action_delay(config)

            except PlaywrightTimeout:
                print(f"  Timeout searching for {keyword}, moving on...")
            except Exception as e:
                print(f"  Error searching for {keyword}: {e}")

    print(f"\nFound {len(companies)} companies total.")
    return companies


def extract_people_from_page(page):
    """Extract people info from current search results using JS evaluation."""
    results = page.eval_on_selector_all(
        'li',
        """els => els.map(el => {
            const link = el.querySelector('a[href*="/in/"]');
            if (!link) return null;
            const href = link.href;
            const allText = el.innerText.trim();
            const lines = allText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
            return { href, lines, text: allText };
        }).filter(x => x !== null)"""
    )
    return results


def find_people_at_company(page, company, config, seen_profiles):
    """Find decision-makers at a given company."""
    people = []
    target_roles = [r.lower() for r in config["target_roles"]]
    max_people = config["max_people_per_company"]
    role_patterns = ["founder", "co-founder", "ceo", "cto", "coo", "cmo", "cpo",
                     "chief executive", "chief technology", "chief operating",
                     "chief product", "chief marketing",
                     "head of engineering", "head of product", "head of technology",
                     "vp of", "vp ", "vice president",
                     "director of engineering", "director of technology",
                     "engineering manager", "tech lead", "technical lead",
                     "managing director"]
    # Words that disqualify a person even if a role keyword matched
    exclude_patterns = [
        # Individual contributors / engineers
        "developer", "engineer", "architect", "programmer", "coder",
        "full stack", "fullstack", "frontend", "backend", "devops engineer",
        "sre", "data scientist", "data analyst", "ml engineer",
        "qa", "tester", "testing",
        # Non-tech / non-decision-maker roles
        "trainer", "trainee", "intern", "student", "faculty",
        "content", "designer", "graphic", "recruiter", "hr ",
        "human resource", "marketing executive", "sales executive",
        "sales rep", "account manager", "account executive",
        "freelancer", "volunteer", "teaching", "teacher",
        "e-learning", "sme", "academician", "instructor",
        "consultant", "analyst", "coordinator", "associate",
        "practitioner", "specialist",
    ]

    print(f"\n  Looking for decision-makers at {company['name']}...")

    # Visit the company's people page directly
    people_url = f"https://www.linkedin.com/company/{company['slug']}/people/"

    try:
        page.goto(people_url, wait_until="domcontentloaded")
        time.sleep(3)
        random_scroll(page)
        time.sleep(2)

        # Try waiting for any profile links
        try:
            page.wait_for_selector('a[href*="/in/"]', timeout=8000)
        except PlaywrightTimeout:
            # Fallback: try search-based approach
            print(f"    No people on company page, trying search...")
            search_url = f"https://www.linkedin.com/search/results/people/?keywords={quote(company['name'])}"
            page.goto(search_url, wait_until="domcontentloaded")
            time.sleep(3)
            random_scroll(page)
            time.sleep(2)
            try:
                page.wait_for_selector('a[href*="/in/"]', timeout=8000)
            except PlaywrightTimeout:
                print(f"    No people found for {company['name']}")
                return people

        raw_people = extract_people_from_page(page)
        print(f"    Extracted {len(raw_people)} people entries from page")

        for entry in raw_people:
            if len(people) >= max_people:
                break

            href = entry.get("href", "")
            match = re.search(r'/in/([^/?]+)', href)
            if not match:
                continue
            profile_url = f"https://www.linkedin.com/in/{match.group(1)}"

            if profile_url in seen_profiles:
                continue

            lines = entry.get("lines", [])
            text = entry.get("text", "").lower()

            # First non-empty line is usually the name
            name = lines[0] if lines else "Unknown"

            # Combine all text lines into one headline for matching
            # LinkedIn headlines are often split across multiple lines
            full_headline = " | ".join(lines[1:6]) if len(lines) > 1 else ""
            headline_lower = full_headline.lower()

            if not headline_lower:
                continue

            # Check if they have a decision-maker role
            matched_role = None
            for role in target_roles + role_patterns:
                if role in headline_lower:
                    matched_role = role
                    break

            if not matched_role:
                continue

            # If they ONLY have excluded roles (developer, engineer, etc.)
            # and no decision-maker title, skip them.
            # But if they're e.g. "DevOps Engineer | Founder" — keep them.
            has_exclude = any(ex in headline_lower for ex in exclude_patterns)
            if has_exclude:
                # Check if their decision-maker role is a strong one (founder, C-level, VP, head)
                strong_roles = ["founder", "co-founder", "ceo", "cto", "coo", "cmo", "cpo",
                                "chief", "head of", "vp ", "vice president", "managing director"]
                has_strong_role = any(sr in headline_lower for sr in strong_roles)
                if not has_strong_role:
                    continue

            headline = full_headline

            person = {
                "name": name,
                "headline": headline,
                "profile_url": profile_url,
                "company": company["name"],
                "company_url": company["url"],
                "matched_role": matched_role,
                "likely_active": len(headline) > 10,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
            }
            person["message"] = generate_message(person)
            people.append(person)
            seen_profiles.add(profile_url)
            print(f"    + {name} — {headline}")

        action_delay(config)

    except PlaywrightTimeout:
        print(f"  Timeout looking at {company['name']}, moving on...")
    except Exception as e:
        print(f"  Error at {company['name']}: {e}")

    return people


def check_profile_activity(page, person, config):
    """Visit profile and activity page to check if they posted 2+ times in last 30 days."""
    try:
        # Visit profile first for connection degree
        page.goto(person["profile_url"], wait_until="domcontentloaded")
        page_delay(config)

        # Try to get connection degree
        degree_badge = page.query_selector('span.dist-value')
        if degree_badge:
            person["connection_degree"] = degree_badge.inner_text().strip()

        # Now visit their activity page to count recent posts
        profile_slug = person["profile_url"].rstrip("/").split("/")[-1]
        activity_url = f"https://www.linkedin.com/in/{profile_slug}/recent-activity/all/"
        page.goto(activity_url, wait_until="domcontentloaded")
        time.sleep(3)
        random_scroll(page)
        time.sleep(2)

        # Extract post dates from the activity feed
        # LinkedIn shows relative times like "1d", "3d", "1w", "2w", "1mo"
        recent_post_count = page.evaluate("""
            () => {
                const now = Date.now();
                const thirtyDaysMs = 30 * 24 * 60 * 60 * 1000;
                const cutoff = now - thirtyDaysMs;
                let count = 0;

                // Look for time elements in activity feed
                const timeEls = document.querySelectorAll('time, span.feed-shared-actor__sub-description span[aria-hidden="true"], span.update-components-actor__sub-description span[aria-hidden="true"]');
                for (const el of timeEls) {
                    const text = el.innerText.trim().toLowerCase();
                    // Parse relative time strings
                    let daysAgo = null;
                    if (text.includes('just now') || text.includes('moment')) daysAgo = 0;
                    else if (text.match(/(\d+)\s*m\b/) && !text.includes('mo')) daysAgo = 0;  // minutes
                    else if (text.match(/(\d+)\s*h/)) daysAgo = 0;  // hours
                    else if (text.match(/(\d+)\s*d/)) daysAgo = parseInt(text.match(/(\d+)\s*d/)[1]);
                    else if (text.match(/(\d+)\s*w/)) daysAgo = parseInt(text.match(/(\d+)\s*w/)[1]) * 7;
                    else if (text.match(/(\d+)\s*mo/)) daysAgo = parseInt(text.match(/(\d+)\s*mo/)[1]) * 30;
                    else if (text.match(/(\d+)\s*yr/)) daysAgo = parseInt(text.match(/(\d+)\s*yr/)[1]) * 365;

                    if (daysAgo !== null && daysAgo <= 30) {
                        count++;
                    }
                }

                // Also try datetime attributes on <time> elements
                const timeTags = document.querySelectorAll('time[datetime]');
                for (const t of timeTags) {
                    const dt = new Date(t.getAttribute('datetime'));
                    if (dt.getTime() > cutoff) {
                        count++;
                    }
                }

                return count;
            }
        """)

        person["recent_posts_30d"] = recent_post_count
        person["has_recent_activity"] = recent_post_count >= 2
        if person["has_recent_activity"]:
            print(f"    [active] {person['name']} — {recent_post_count} posts in last 30 days")
        else:
            print(f"    [inactive] {person['name']} — only {recent_post_count} posts in last 30 days, skipping connect")

    except Exception as e:
        print(f"    [activity] Error checking {person['name']}: {e}")
        person["has_recent_activity"] = None
        person["recent_posts_30d"] = 0

    return person


def send_connection_request(page, person, config):
    """Send a connection request with a personalized note from the profile page."""
    try:
        # Make sure we're on their profile
        if page.url.rstrip("/") != person["profile_url"].rstrip("/"):
            page.goto(person["profile_url"], wait_until="domcontentloaded")
            page_delay(config)

        message = person.get("message", "")
        if not message:
            print(f"    [connect] No message for {person['name']}, skipping")
            return False

        print(f"\n    [connect] Sending invitation to {person['name']} at {person['company']}")
        print(f"    [connect] Role: {person.get('matched_role', 'unknown')}")
        print(f"    [connect] Message: {message}")

        # Try clicking the main "Connect" button
        connect_clicked = False
        connect_btn = page.query_selector('button:has-text("Connect")')
        if connect_btn:
            connect_btn.click()
            connect_clicked = True
        else:
            # Try the "More" dropdown
            more_btn = page.query_selector('button[aria-label="More actions"]')
            if more_btn:
                more_btn.click()
                time.sleep(1)
                connect_option = page.query_selector('div[role="listbox"] span:has-text("Connect"), div.artdeco-dropdown__content span:has-text("Connect")')
                if connect_option:
                    connect_option.click()
                    connect_clicked = True
                else:
                    page.keyboard.press("Escape")

        if not connect_clicked:
            print(f"    [connect] No Connect button for {person['name']}")
            person["connect_sent"] = False
            return False

        time.sleep(2)

        # Look for "Add a note" button in the modal
        add_note_btn = page.query_selector('button:has-text("Add a note")')
        if add_note_btn:
            add_note_btn.click()
            time.sleep(1)

            # Type the message in the note textarea
            note_field = page.query_selector('textarea[name="message"], textarea#custom-message')
            if not note_field:
                # Try a broader selector
                note_field = page.query_selector('textarea')

            if note_field:
                note_field.fill(message)
                time.sleep(1)

                # Click Send
                send_btn = page.query_selector('button:has-text("Send")')
                if send_btn:
                    send_btn.click()
                    time.sleep(2)
                    print(f"    [connect] SENT to {person['name']} at {person['company']}")
                    person["connect_sent"] = True
                    return True
            else:
                print(f"    [connect] Could not find note field for {person['name']}")
                page.keyboard.press("Escape")
        else:
            # No "Add a note" option — might be a direct send modal
            # Try to find and click Send directly, or dismiss
            send_btn = page.query_selector('button:has-text("Send")')
            if send_btn:
                # Dismiss and skip — we want to send with a note
                page.keyboard.press("Escape")
                print(f"    [connect] No 'Add a note' option for {person['name']}, skipped")

        person["connect_sent"] = False
        return False

    except Exception as e:
        print(f"    [connect] Error sending to {person['name']}: {e}")
        person["connect_sent"] = False
        return False


FIELDNAMES = [
    "name", "profile_url", "company", "company_url",
    "matched_role", "has_recent_activity", "recent_posts_30d",
    "connection_degree", "found_date", "message", "connect_sent",
]

_MESSAGE_TEMPLATES = [
    "Hey {first_name}! Stumbled on {company} and thought it looked really cool. I'm a dev who works with small teams — backend, cloud, DevOps stuff. Would be great to connect!",
    "Hey {first_name}, came across {company} and liked what you guys are up to. I do backend and cloud work with small teams. Let's connect!",
    "Hey {first_name}! Been checking out {company} — cool stuff. I'm into backend dev, AWS, Kubernetes, that kinda thing. Would love to connect!",
    "Hey {first_name}, saw {company} and had to reach out. I work with small teams on backend and infra — always cool to meet folks building interesting things. Let's connect!",
    "Hey {first_name}! {company} looks awesome. I'm a dev who does backend, cloud, and DevOps with small teams. Would love to be in your network!",
]


def generate_message(person):
    """Generate a personalized connection request message under 300 chars."""
    first_name = person["name"].split()[0] if person["name"] else "there"
    company = person["company"]
    # Shorten company name if too long
    if len(company) > 40:
        company = company[:37] + "..."

    template = random.choice(_MESSAGE_TEMPLATES)
    msg = template.format(first_name=first_name, company=company)

    # Ensure under 300 chars
    if len(msg) > 300:
        msg = msg[:297] + "..."
    return msg


def save_prospects(prospects, config):
    """Save prospects to CSV and optionally sync to Google Sheets."""
    output_file = SCRIPT_DIR / config["output_file"]
    file_exists = output_file.exists()

    with open(output_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for person in prospects:
            writer.writerow(person)

    print(f"\nSaved {len(prospects)} prospects to {output_file}")

    # Sync to Google Sheets if configured
    gs_config = config.get("google_sheets", {})
    if gs_config.get("enabled") and gs_config.get("sheet_url"):
        sync_to_google_sheets(config)


def sync_to_google_sheets(config):
    """Read full CSV and sync it to Google Sheets (append new rows, skip duplicates)."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("  [sheets] gspread not installed. Run: pip install gspread google-auth")
        return

    gs_config = config["google_sheets"]
    # Resolve relative path from script directory
    key_path = gs_config["service_account_key"]
    if not os.path.isabs(key_path):
        key_path = str(SCRIPT_DIR / key_path)
    key_path = os.path.expanduser(key_path)

    if not os.path.exists(key_path):
        print(f"  [sheets] Service account key not found at {key_path}")
        return

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(key_path, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_url(gs_config["sheet_url"]).sheet1

        # Get existing profile URLs from sheet to avoid duplicates
        existing_data = sheet.get_all_values()
        if existing_data:
            header = existing_data[0]
            if "profile_url" in header:
                url_col = header.index("profile_url")
                existing_urls = {row[url_col] for row in existing_data[1:] if len(row) > url_col}
            else:
                existing_urls = set()
                sheet.update(values=[FIELDNAMES], range_name="A1")
        else:
            existing_urls = set()
            sheet.update(values=[FIELDNAMES], range_name="A1")

        # Read all prospects from CSV
        output_file = SCRIPT_DIR / config["output_file"]
        new_rows = []
        with open(output_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("profile_url") not in existing_urls:
                    new_rows.append([row.get(field, "") for field in FIELDNAMES])

        if new_rows:
            next_row = len(existing_data) + 1 if existing_data else 2
            sheet.update(values=new_rows, range_name=f"A{next_row}")
            print(f"  [sheets] Added {len(new_rows)} new rows to Google Sheet")
        else:
            print(f"  [sheets] No new rows to add (all already in sheet)")

    except Exception as e:
        print(f"  [sheets] Error syncing to Google Sheets: {e}")


def do_search(playwright, config, auto_connect=False):
    """Run the full search pipeline."""
    session_file = SESSION_DIR / "state.json"
    if not session_file.exists():
        print("No saved session found. Run with --login first.")
        sys.exit(1)

    print("\n--- LinkedIn Prospector ---")
    print(f"Max companies: {config['max_companies_per_run']}")
    print(f"Max people per company: {config['max_people_per_company']}")
    print(f"Delays: {config['delay_between_actions']['min_seconds']}-{config['delay_between_actions']['max_seconds']}s between actions")

    browser = playwright.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        storage_state=str(session_file),
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    seen_profiles = load_seen_profiles()
    all_prospects = []

    try:
        # Verify session is still valid
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        time.sleep(3)
        if "login" in page.url:
            print("Session expired. Run with --login to re-authenticate.")
            browser.close()
            sys.exit(1)

        print("Session valid. Starting search...\n")

        # Step 1: Find companies
        companies = search_companies(page, config)

        # Step 2: Find people at each company
        for company in companies:
            people = find_people_at_company(page, company, config, seen_profiles)

            # Step 3: Check profile activity
            for person in people:
                if person["likely_active"]:
                    person = check_profile_activity(page, person, config)
                    # Step 4: Only send connect to active decision makers (2+ posts in 30 days)
                    if auto_connect and person.get("has_recent_activity"):
                        send_connection_request(page, person, config)
                    page_delay(config)

            all_prospects.extend(people)

        # Save results
        if all_prospects:
            save_prospects(all_prospects, config)
            save_seen_profiles(seen_profiles)
        else:
            print("\nNo matching prospects found this run. Try adjusting search keywords in config.json.")

        # Update session in case cookies were refreshed
        context.storage_state(path=str(session_file))

    except KeyboardInterrupt:
        print("\n\nInterrupted! Saving what we have so far...")
        if all_prospects:
            save_prospects(all_prospects, config)
            save_seen_profiles(seen_profiles)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        if all_prospects:
            save_prospects(all_prospects, config)
            save_seen_profiles(seen_profiles)
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Prospector - Find contract job opportunities")
    parser.add_argument("--login", action="store_true", help="Open browser for manual LinkedIn login")
    parser.add_argument("--search", action="store_true", help="Run the company/people search (default if no flags)")
    parser.add_argument("--connect", action="store_true", help="Auto-send connection requests with personalized notes")
    args = parser.parse_args()

    # Default to search if no flags given
    if not args.login and not args.search and not args.connect:
        args.search = True

    # --connect implies --search
    if args.connect:
        args.search = True

    config = load_config()
    SESSION_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        if args.login:
            do_login(p)
        if args.search:
            do_search(p, config, auto_connect=args.connect)


if __name__ == "__main__":
    main()
