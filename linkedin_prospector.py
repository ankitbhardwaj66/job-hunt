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

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")

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


def _get_sheets_client(config):
    """Get authenticated gspread client and sheet."""
    import gspread
    from google.oauth2.service_account import Credentials

    gs_config = config.get("google_sheets", {})
    key_path = gs_config.get("service_account_key", "")
    if not os.path.isabs(key_path):
        key_path = str(SCRIPT_DIR / key_path)
    key_path = os.path.expanduser(key_path)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_file(key_path, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_url(gs_config["sheet_url"]).sheet1
    return sheet


def _load_existing_from_sheet(config):
    """Load existing data from Google Sheet — company slugs and profile URLs."""
    slugs = set()
    profile_urls = set()
    try:
        sheet = _get_sheets_client(config)
        data = sheet.get_all_values()
        if not data:
            return slugs, profile_urls

        # Scan ALL cells for company slugs and profile URLs
        # This handles misaligned headers from schema changes
        for row in data[1:]:
            for cell in row:
                cell = cell.strip()
                if not cell:
                    continue
                # Extract company slugs from any cell containing /company/
                company_match = re.search(r'/company/([^/?]+)', cell)
                if company_match:
                    slugs.add(company_match.group(1))
                # Extract profile URLs from any cell containing /in/
                if '/in/' in cell and 'linkedin.com' in cell:
                    profile_urls.add(cell.split("?")[0])

        print(f"Loaded from Google Sheet: {len(slugs)} companies, {len(profile_urls)} profiles")
    except Exception as e:
        print(f"Warning: Could not load from Google Sheet: {e}")
        print("Continuing without dedup...")

    return slugs, profile_urls


def search_companies(page, config):
    """Search LinkedIn for small tech companies and return company info."""
    companies = []
    seen_companies = config.get("_existing_slugs", set())
    max_companies = config["max_companies_per_run"]
    keywords = config["search_keywords"]
    geo_id = config.get("_geo_id", "")  # set by do_search for local mode

    random.shuffle(keywords)

    for keyword in keywords:
        if len(companies) >= max_companies:
            break

        print(f"\nSearching for: {keyword}")

        for size_code in ["C"]:  # C = 11-50 employees
            if len(companies) >= max_companies:
                break

            url = f"https://www.linkedin.com/search/results/companies/?keywords={quote(keyword)}&companySize=%5B%22{size_code}%22%5D"
            if geo_id:
                url += f"&companyHqGeo=%5B%22{geo_id}%22%5D"

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
                        print(f"    [skip] {comp['name']} — already prospected")
                        continue

                    orig_name = comp["name"].strip()
                    name_lower = orig_name.lower()

                    # Skip 0: Companies in the skip list (e.g. past employers)
                    skip_companies = [s.lower() for s in config.get("skip_companies", [])]
                    if any(sc in name_lower for sc in skip_companies):
                        print(f"    [skip] {orig_name} — in skip list")
                        continue

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

                    # Skip 4: Company name mentions a country — usually spammy/outsourcing
                    country_names = {
                        "usa", "india", "uk", "china", "japan", "korea", "vietnam",
                        "singapore", "indonesia", "malaysia", "thailand", "philippines",
                        "pakistan", "bangladesh", "nepal", "germany", "france", "spain",
                        "italy", "netherlands", "sweden", "norway", "denmark", "finland",
                        "poland", "switzerland", "australia", "canada", "brazil", "mexico",
                        "argentina", "nigeria", "kenya", "south africa", "egypt", "uae",
                        "dubai", "israel", "turkey", "russia", "ukraine", "ireland",
                    }
                    name_words_lower = set(name_lower.replace("-", " ").replace(".", " ").split())
                    if name_words_lower & country_names:
                        print(f"    [skip] {orig_name} — contains country name")
                        continue

                    # Skip 5: Check full name for disqualifying org types
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


def find_people_at_company(page, company, config, seen_profiles, local_mode=False, location=""):
    """Find decision-makers at a given company."""
    people = []
    target_roles = [r.lower() for r in config["target_roles"]]
    max_people = config["max_people_per_company"]
    # Ordered by priority — technical decision-makers first, then business roles
    role_patterns = [
        # Priority 1: Technical managers — most likely to hire contract devs
        "cto", "chief technology",
        "engineering manager", "tech lead", "technical lead",
        "head of engineering", "head of technology", "head of product",
        "vp of engineering", "vp of technology",
        "director of engineering", "director of technology",
        # Priority 2: Other C-level / VP
        "coo", "chief operating", "cmo", "cpo",
        "chief product", "chief marketing",
        "vp of", "vp ", "vice president",
        "managing director",
        # Priority 3: Business roles — CEO/Founder (may not be technical)
        "ceo", "chief executive",
        "founder", "co-founder",
    ]
    # Map roles to priority numbers (lower = better)
    _role_priority = {}
    for i, role in enumerate(role_patterns):
        _role_priority[role] = i
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
        # Job seekers — they're looking for work, not hiring
        "available for", "looking for", "seeking", "open to work",
        "actively looking", "job search", "hire me",
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
            person["message"] = generate_message(person, local_mode=local_mode, location=location)
            person["local"] = "yes" if local_mode else "no"
            people.append(person)
            seen_profiles.add(profile_url)
            print(f"    + {name} — {headline}")

        action_delay(config)

    except PlaywrightTimeout:
        print(f"  Timeout looking at {company['name']}, moving on...")
    except Exception as e:
        print(f"  Error at {company['name']}: {e}")

    # Sort by role priority — CTOs and engineering managers first
    people.sort(key=lambda p: _role_priority.get(p.get("matched_role", ""), 99))
    # Limit to max_people after sorting
    if len(people) > max_people:
        people = people[:max_people]

    return people


def check_profile_activity(page, person, config, local_mode=False):
    """Visit profile activity page — check for any 2+ activities in last 30 days (60 for local)."""
    try:
        # Visit profile first for connection degree
        page.goto(person["profile_url"], wait_until="domcontentloaded")
        page_delay(config)

        # Try to get connection degree
        degree_badge = page.query_selector('span.dist-value')
        if degree_badge:
            person["connection_degree"] = degree_badge.inner_text().strip()

        # Visit "All activity" page — shows posts, comments, and reactions
        profile_slug = person["profile_url"].rstrip("/").split("/")[-1]
        activity_url = f"https://www.linkedin.com/in/{profile_slug}/recent-activity/all/"
        page.goto(activity_url, wait_until="domcontentloaded")
        time.sleep(3)
        random_scroll(page)
        time.sleep(2)

        # Count any activity — 30 days for global, 60 days for local
        activity_days = 60 if local_mode else 30
        recent_activity_count = page.evaluate("""
            (days) => {
                let count = 0;

                // Helper: parse relative time text to days ago
                function parseDaysAgo(text) {
                    text = text.toLowerCase().trim();
                    if (text.includes('just now') || text.includes('moment')) return 0;
                    let m;
                    if ((m = text.match(/(\\d+)\\s*m\\b/)) && !text.includes('mo')) return 0;
                    if ((m = text.match(/(\\d+)\\s*h/))) return 0;
                    if ((m = text.match(/(\\d+)\\s*d/))) return parseInt(m[1]);
                    if ((m = text.match(/(\\d+)\\s*w/))) return parseInt(m[1]) * 7;
                    if ((m = text.match(/(\\d+)\\s*mo/))) return parseInt(m[1]) * 30;
                    if ((m = text.match(/(\\d+)\\s*yr/))) return parseInt(m[1]) * 365;
                    return null;
                }

                // Strategy 1: All activity feed items (posts, shares, comments, reactions)
                // Each feed item has a timestamp somewhere
                const allText = document.querySelectorAll(
                    'time, ' +
                    'span[aria-hidden="true"], ' +
                    '.feed-shared-actor__sub-description, ' +
                    '.update-components-actor__sub-description'
                );
                const seen = new Set();
                for (const el of allText) {
                    const text = el.innerText.trim();
                    if (seen.has(text) || text.length > 50) continue;
                    seen.add(text);

                    const daysAgo = parseDaysAgo(text);
                    if (daysAgo !== null && daysAgo <= days) {
                        count++;
                    }
                }

                // Strategy 2: <time> elements with datetime attributes
                const cutoff = Date.now() - (days * 24 * 60 * 60 * 1000);
                const timeTags = document.querySelectorAll('time[datetime]');
                for (const t of timeTags) {
                    const key = t.getAttribute('datetime');
                    if (seen.has(key)) continue;
                    seen.add(key);
                    const dt = new Date(key);
                    if (dt.getTime() > cutoff) {
                        count++;
                    }
                }

                return count;
            }
        """, activity_days)

        person["recent_activity_30d"] = recent_activity_count
        person["has_recent_activity"] = recent_activity_count >= 2
        if person["has_recent_activity"]:
            print(f"    [active] {person['name']} — {recent_activity_count} activities in last {activity_days} days")
        else:
            print(f"    [inactive] {person['name']} — only {recent_activity_count} activities in last {activity_days} days, skipping connect")

    except Exception as e:
        print(f"    [activity] Error checking {person['name']}: {e}")
        person["has_recent_activity"] = None
        person["recent_activity_30d"] = 0

    return person


def send_connection_request(page, person, config):
    """Navigate to profile and send a connection request with a personalized note."""
    try:
        message = person.get("message", "")
        if not message:
            print(f"    [connect] No message for {person['name']}, skipping")
            return False

        # ALWAYS navigate to the profile page first to avoid clicking wrong buttons
        print(f"\n    [connect] Navigating to {person['name']}'s profile...")
        page.goto(person["profile_url"], wait_until="domcontentloaded")
        time.sleep(3)

        # Extract first name for aria-label matching
        first_name = person["name"].split()[0].lower() if person["name"] else ""
        last_name = person["name"].split()[-1].lower() if person["name"] and len(person["name"].split()) > 1 else ""

        print(f"    [connect] Sending invitation to {person['name']} at {person['company']}")
        print(f"    [connect] Role: {person.get('matched_role', 'unknown')}")
        print(f"    [connect] Message: {message}")

        # Use JS to find the Connect button that belongs to THIS person
        # The aria-label contains the person's name e.g. "Invite John Doe to connect"
        connect_clicked = page.evaluate("""
            (names) => {
                const firstName = names.first;
                const lastName = names.last;
                const buttons = document.querySelectorAll('button');
                // First pass: find Connect button with matching name in aria-label
                for (const btn of buttons) {
                    const text = btn.innerText.trim();
                    const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if (text === 'Connect' && btn.offsetParent !== null) {
                        // Check if aria-label contains the person's name
                        if (ariaLabel.includes(firstName) && (lastName === '' || ariaLabel.includes(lastName))) {
                            btn.click();
                            return 'matched: ' + ariaLabel;
                        }
                    }
                }
                // Second pass: check the main profile action buttons area only
                // These are typically in the first section before the feed
                const mainSection = document.querySelector('.pv-top-card-v2-ctas, .pv-top-card__cta-container, .pvs-profile-actions');
                if (mainSection) {
                    const btns = mainSection.querySelectorAll('button');
                    for (const btn of btns) {
                        if (btn.innerText.trim() === 'Connect' && btn.offsetParent !== null) {
                            btn.click();
                            return 'main_section';
                        }
                    }
                }
                return null;
            }
        """, {"first": first_name, "last": last_name})

        if not connect_clicked:
            # Try the "More" dropdown on the profile (Connect is sometimes hidden there)
            print(f"    [connect] No main Connect button, trying More dropdown...")
            # Find the More button near the top of the profile (not sidebar ones)
            more_btn = page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {
                        const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                        const text = btn.innerText.trim();
                        if ((aria === 'more actions' || text === 'More') && btn.offsetParent !== null) {
                            // Make sure it's near the top of the page (profile actions area)
                            const rect = btn.getBoundingClientRect();
                            if (rect.y < 500) {
                                btn.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """)
            if more_btn:
                time.sleep(2)
                # Take screenshot for debugging
                DEBUG_DIR.mkdir(exist_ok=True)
                page.screenshot(path=str(DEBUG_DIR / "dropdown_open.png"), full_page=False)

                # Use Playwright's native click — much more reliable than JS for dropdowns
                try:
                    # Look for "Connect" text in the dropdown using Playwright locators
                    connect_item = page.locator('text="Connect"').first
                    if connect_item.is_visible(timeout=3000):
                        connect_item.click()
                        connect_clicked = "dropdown"
                        time.sleep(2)
                except Exception:
                    pass

                if not connect_clicked:
                    page.keyboard.press("Escape")

        if not connect_clicked:
            print(f"    [connect] No Connect button found for {person['name']}")
            person["connect_sent"] = False
            return False

        print(f"    [connect] Clicked Connect ({connect_clicked}), waiting for modal...")
        # Longer wait if connect came from dropdown
        wait_time = 5 if "dropdown" in str(connect_clicked) else 3
        time.sleep(wait_time)

        # Look for "Add a note" button in the modal — try multiple times
        add_note_btn = False
        max_attempts = 5 if "dropdown" in str(connect_clicked) else 3
        for attempt in range(max_attempts):
            add_note_btn = page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {
                        const text = btn.innerText.trim().toLowerCase();
                        if ((text.includes('add a note') || text.includes('add note'))
                            && btn.offsetParent !== null) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if add_note_btn:
                break
            print(f"    [connect] Waiting for 'Add a note' button (attempt {attempt + 1}/{max_attempts})...")
            time.sleep(2)

        if add_note_btn:
            time.sleep(1.5)
            # Find the textarea in the modal and type the message
            note_field = page.query_selector('textarea[name="message"], textarea#custom-message, textarea.connect-button-send-invite__custom-message')
            if not note_field:
                note_field = page.query_selector('div[role="dialog"] textarea, .artdeco-modal textarea')
            if not note_field:
                note_field = page.query_selector('textarea')

            if note_field:
                note_field.fill(message)
                time.sleep(1)

                # Click Send in the modal
                sent = page.evaluate("""
                    () => {
                        const buttons = document.querySelectorAll('button');
                        for (const btn of buttons) {
                            const text = btn.innerText.trim().toLowerCase();
                            if ((text === 'send' || text === 'send invitation') && btn.offsetParent !== null) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)

                if sent:
                    time.sleep(2)
                    print(f"    [connect] SENT to {person['name']} at {person['company']}")
                    person["connect_sent"] = True
                    return True
                else:
                    print(f"    [connect] Could not find Send button for {person['name']}")
                    page.keyboard.press("Escape")
            else:
                print(f"    [connect] Could not find note field for {person['name']}")
                page.keyboard.press("Escape")
        else:
            # No "Add a note" — check if LinkedIn sent it without a note
            page.keyboard.press("Escape")
            time.sleep(1)
            pending = page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {
                        if (btn.innerText.trim().toLowerCase() === 'pending' && btn.offsetParent !== null) {
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if pending:
                print(f"    [connect] SENT to {person['name']} at {person['company']} (without note)")
                person["connect_sent"] = "sent_no_note"
                return True
            else:
                print(f"    [connect] No 'Add a note' option for {person['name']}, skipped")

        person["connect_sent"] = False
        return False

    except Exception as e:
        print(f"    [connect] Error sending to {person['name']}: {e}")
        # Try to dismiss any open modals
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        person["connect_sent"] = False
        return False


FIELDNAMES = [
    "name", "company",
    "matched_role", "has_recent_activity", "recent_activity_30d",
    "connection_degree", "found_date", "message", "connect_sent", "local",
]

_SALUTATIONS = {
    "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "dr", "dr.", "prof", "prof.",
    "sir", "shri", "smt", "ca", "er", "er.", "ca.",
}


def _clean_first_name(name):
    """Extract first name, skipping salutations like Mr., Mrs., Dr."""
    parts = name.split() if name else []
    for part in parts:
        if part.lower().rstrip(".") not in {s.rstrip(".") for s in _SALUTATIONS}:
            return part
    return parts[-1] if parts else "there"


def _generate_message_ai(person, local_mode=False, location=""):
    """Use Claude to draft a natural, casual connection request under 300 chars."""
    try:
        import anthropic
        client = anthropic.Anthropic()

        first_name = _clean_first_name(person["name"])
        company = person["company"]
        role = person.get("matched_role", "")
        headline = person.get("headline", "")

        local_context = ""
        if local_mode and location:
            local_context = f"""
- I also live in {location} and work from home
- Mention we're in the same city casually
- Mention I'm open for contract work"""

        prompt = f"""Write a LinkedIn connection request note. MUST be under 300 characters total.

About me: Ankit, software engineer (backend, DevOps, AWS, Kubernetes), work from home, open to contract work.

About them:
- Name: {first_name}
- Found at company: {company}
- Role listed: {role}
- Their actual LinkedIn headline: {headline}
{local_context}

IMPORTANT: Check if their headline matches the company I found them at.
- If their headline mentions a DIFFERENT company or project (e.g. headline says "Building Bolo Buddy" but company is "Brain Point"), they probably moved on. Reference what's in their HEADLINE instead, not the old company.
- If the headline matches the company, reference the company name (use short/common name only, not full legal name or taglines).

Rules:
- MUST be under 300 characters (this is a hard LinkedIn limit)
- Start with "Hey {first_name}"
- Sound like a real person texting, not a recruiter or bot
- Keep it casual and friendly
- Don't use corporate words like "synergy", "leverage", "opportunity"
- Don't mention my website
- Do NOT use any emojis
- End with something like "Would love to connect!" or "Let's connect!"
- Just output the message, nothing else"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        msg = response.content[0].text.strip().strip('"')

        # Safety check
        if len(msg) > 300:
            msg = msg[:297] + "..."
        return msg

    except Exception as e:
        print(f"    [ai] Error generating message: {e}")
        return None


def _generate_message_fallback(person, local_mode=False, location=""):
    """Fallback template-based message if AI is unavailable."""
    first_name = _clean_first_name(person["name"])
    company = person["company"].split(" - ")[0].split(" | ")[0].strip()
    if len(company) > 30:
        company = company[:27] + "..."

    if local_mode and location:
        msg = f"Hey {first_name}! Fellow {location} person here. Saw {company} and liked what you're doing. I'm a software engineer, WFH, open for contract work. Would love to connect locally!"
    else:
        msg = f"Hey {first_name}! Came across {company} and liked what you guys are up to. I'm a dev who works with small teams — backend, cloud, DevOps. Would love to connect!"

    if len(msg) > 300:
        msg = msg[:297] + "..."
    return msg


def generate_message(person, local_mode=False, location=""):
    """Generate a personalized connection request message under 300 chars."""
    # Try AI first, fall back to template
    msg = _generate_message_ai(person, local_mode, location)
    if not msg:
        msg = _generate_message_fallback(person, local_mode, location)
    return msg


def save_prospects(prospects, config):
    """Save prospects directly to Google Sheet."""
    if not prospects:
        print("\nNo new prospects to save.")
        return

    try:
        sheet = _get_sheets_client(config)
        existing_data = sheet.get_all_values()

        # Ensure header exists
        if not existing_data:
            sheet.update(values=[FIELDNAMES], range_name="A1")
            existing_data = [FIELDNAMES]

        # Get existing profile URLs to avoid duplicates
        header = existing_data[0]
        if "profile_url" in header:
            url_col = header.index("profile_url")
            existing_urls = {row[url_col] for row in existing_data[1:] if len(row) > url_col}
        else:
            existing_urls = set()

        # Build new rows (skip duplicates)
        new_rows = []
        for person in prospects:
            if person.get("profile_url") not in existing_urls:
                row = []
                for field in FIELDNAMES:
                    val = str(person.get(field, ""))
                    # Make name a hyperlink to profile
                    if field == "name" and person.get("profile_url"):
                        val = f'=HYPERLINK("{person["profile_url"]}","{val.replace(chr(34), chr(39))}")'
                    # Make company a hyperlink to company page
                    elif field == "company" and person.get("company_url"):
                        val = f'=HYPERLINK("{person["company_url"]}","{val.replace(chr(34), chr(39))}")'
                    row.append(val)
                new_rows.append(row)

        if new_rows:
            next_row = len(existing_data) + 1
            sheet.update(values=new_rows, range_name=f"A{next_row}", value_input_option="USER_ENTERED")
            print(f"\n  [sheets] Added {len(new_rows)} new prospects to Google Sheet")
        else:
            print(f"\n  [sheets] No new prospects to add (all already in sheet)")

    except Exception as e:
        print(f"\n  [sheets] Error saving to Google Sheet: {e}")
        # Fallback: save to local CSV so data isn't lost
        print("  [sheets] Saving to local CSV as fallback...")
        output_file = SCRIPT_DIR / "prospects_fallback.csv"
        file_exists = output_file.exists()
        with open(output_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            for person in prospects:
                writer.writerow(person)
        print(f"  Saved {len(prospects)} prospects to {output_file}")


def do_search(playwright, config, auto_connect=False, local_mode=False):
    """Run the full search pipeline."""
    session_file = SESSION_DIR / "state.json"
    if not session_file.exists():
        print("No saved session found. Run with --login first.")
        sys.exit(1)

    # Override keywords and set geo filter if local mode
    location = ""
    if local_mode:
        local_config = config.get("local_mode", {})
        location = local_config.get("location", "")
        geo_id = local_config.get("geo_id", "")
        config = {**config}
        if local_config.get("search_keywords"):
            config["search_keywords"] = local_config["search_keywords"]
        if geo_id:
            config["_geo_id"] = geo_id
        print(f"\n--- LinkedIn Prospector [LOCAL: {location}] ---")
    else:
        print("\n--- LinkedIn Prospector ---")

    # Load existing data from Google Sheet
    existing_slugs, existing_profiles = _load_existing_from_sheet(config)
    config["_existing_slugs"] = existing_slugs

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

    seen_profiles = existing_profiles  # from Google Sheet
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
            people = find_people_at_company(page, company, config, seen_profiles,
                                            local_mode=local_mode, location=location)

            if not people:
                # No decision-makers found — add a placeholder so we skip this company next run
                all_prospects.append({
                    "name": "no_contact_found",
                    "profile_url": "",
                    "company": company["name"],
                    "company_url": company["url"],
                    "matched_role": "",
                    "has_recent_activity": "",
                    "recent_activity_30d": "",
                    "connection_degree": "",
                    "found_date": datetime.now().strftime("%Y-%m-%d"),
                    "message": "",
                    "connect_sent": "",
                    "local": "yes" if local_mode else "no",
                })
                print(f"    No decision-makers found, marking company as visited")
            else:
                # Step 3: Check profile activity
                for person in people:
                    if person["likely_active"]:
                        person = check_profile_activity(page, person, config, local_mode=local_mode)
                        # Step 4: Only send connect to active decision makers
                        if auto_connect and person.get("has_recent_activity"):
                            send_connection_request(page, person, config)
                        page_delay(config)

            all_prospects.extend(people)

        # Save results
        if all_prospects:
            save_prospects(all_prospects, config)
            pass  # profiles tracked in Google Sheet
        else:
            print("\nNo matching prospects found this run. Try adjusting search keywords in config.json.")

        # Update session in case cookies were refreshed
        context.storage_state(path=str(session_file))

    except KeyboardInterrupt:
        print("\n\nInterrupted! Saving what we have so far...")
        if all_prospects:
            save_prospects(all_prospects, config)
            pass  # profiles tracked in Google Sheet
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        if all_prospects:
            save_prospects(all_prospects, config)
            pass  # profiles tracked in Google Sheet
    finally:
        try:
            browser.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Prospector - Find contract job opportunities")
    parser.add_argument("--login", action="store_true", help="Open browser for manual LinkedIn login")
    parser.add_argument("--search", action="store_true", help="Run the company/people search (default if no flags)")
    parser.add_argument("--connect", action="store_true", help="Auto-send connection requests with personalized notes")
    parser.add_argument("--local", action="store_true", help="Chandigarh mode — target local companies with local messaging")
    args = parser.parse_args()

    # Default to search if no flags given
    if not args.login and not args.search and not args.connect and not args.local:
        args.search = True

    # --connect or --local implies --search
    if args.connect or args.local:
        args.search = True

    config = load_config()
    SESSION_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        if args.login:
            do_login(p)
        if args.search:
            do_search(p, config, auto_connect=args.connect, local_mode=args.local)


if __name__ == "__main__":
    main()
