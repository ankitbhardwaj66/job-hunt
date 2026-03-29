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
INDUSTRY_STATE_FILE = SCRIPT_DIR / ".industry_state.json"


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

        # Get formulas (not display values) since URLs are in HYPERLINK formulas
        # get_all_values() returns display text, we need the actual formulas
        formulas = sheet.get(value_render_option="FORMULA")
        if not formulas:
            return slugs, profile_urls

        for row in formulas[1:]:
            for cell in row:
                cell = str(cell).strip()
                if not cell:
                    continue
                # Extract company slugs from HYPERLINK formulas or raw URLs
                company_match = re.search(r'/company/([^/?\"]+)', cell)
                if company_match:
                    slugs.add(company_match.group(1))
                # Extract profile URLs from HYPERLINK formulas or raw URLs
                profile_match = re.search(r'(https?://[^"]*linkedin\.com/in/[^"/?]+)', cell)
                if profile_match:
                    profile_urls.add(profile_match.group(1))

        print(f"Loaded from Google Sheet: {len(slugs)} companies, {len(profile_urls)} profiles")
    except Exception as e:
        print(f"Warning: Could not load from Google Sheet: {e}")
        print("Continuing without dedup...")

    return slugs, profile_urls


def search_companies(page, config):
    """Search LinkedIn for small tech companies using faceted filters (no keyword search)."""
    companies = []
    seen_companies = config.get("_existing_slugs", set())
    max_companies = config["max_companies_per_run"]

    industry_codes = config.get("industry_codes", ["96", "4"])
    size_codes = config.get("company_size_codes", ["C"])
    geo_id = config.get("_geo_id", "")

    # Pick one industry per run, rotating through the list
    if INDUSTRY_STATE_FILE.exists():
        with open(INDUSTRY_STATE_FILE) as f:
            state = json.load(f)
        current_index = state.get("last_index", 0)
    else:
        current_index = 0
    current_index = current_index % len(industry_codes)
    next_index = (current_index + 1) % len(industry_codes)
    current_industry = industry_codes[current_index]
    with open(INDUSTRY_STATE_FILE, "w") as f:
        json.dump({"last_index": next_index}, f)

    industry_names = {
        "96": "IT Services & Consulting", "4": "Software Development",
        "6": "Tech, Info & Internet", "3": "Tech, Info & Media",
        "48": "Computer & Network Security", "5": "Computer Networking",
        "2458": "Data Infrastructure & Analytics",
    }
    industry_label = industry_names.get(current_industry, f"industry {current_industry}")
    next_label = industry_names.get(industry_codes[next_index], f"industry {industry_codes[next_index]}")

    # Build faceted search URL with just the current industry
    industry_param = quote(json.dumps([current_industry], separators=(",", ":")))
    size_param = quote(json.dumps(size_codes, separators=(",", ":")))
    base_url = (
        f"https://www.linkedin.com/search/results/companies/"
        f"?origin=FACETED_SEARCH"
        f"&companySize={size_param}"
        f"&industryCompanyVertical={industry_param}"
    )
    if geo_id:
        geo_param = quote(json.dumps([geo_id], separators=(",", ":")))
        base_url += f"&companyHqGeo={geo_param}"

    print(f"\nSearching industry: {industry_label} (code {current_industry}) [{current_index + 1}/{len(industry_codes)}]")
    print(f"Next run will search: {next_label}")

    # Paginate through results (up to 10 pages = ~100 companies)
    for page_num in range(1, 11):
        if len(companies) >= max_companies:
            break

        url = base_url if page_num == 1 else f"{base_url}&page={page_num}"

        try:
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(3)
            random_scroll(page)
            time.sleep(2)
            random_scroll(page)
            time.sleep(1)

            try:
                page.wait_for_selector('a[href*="/company/"]', timeout=8000)
            except PlaywrightTimeout:
                if page_num == 1:
                    print("  No company links found — check industry/size codes or session")
                    debug_snapshot(page, "no_results_faceted")
                break  # No more pages

            page_companies = extract_companies_from_page(page)
            if not page_companies:
                break  # No more results
            print(f"  Page {page_num}: {len(page_companies)} companies found")

            skip_companies_list = [s.lower() for s in config.get("skip_companies", [])]

            for comp in page_companies:
                if len(companies) >= max_companies:
                    break
                if comp["slug"] in seen_companies:
                    print(f"    [skip] {comp['name']} — already prospected")
                    continue

                orig_name = comp["name"].strip()
                name_lower = orig_name.lower()

                if any(sc in name_lower for sc in skip_companies_list):
                    print(f"    [skip] {orig_name} — in skip list")
                    continue

                if name_lower.startswith("page by"):
                    print(f"    [skip] {orig_name} — LinkedIn page, not a company")
                    continue

                # Stealth / placeholder LinkedIn entries — not real contactable companies
                placeholder_names = {"stealth", "startup", "a startup", "stealth startup",
                                     "new startup", "tech startup", "my startup", "our startup"}
                if "stealth" in name_lower or name_lower.strip() in placeholder_names:
                    print(f"    [skip] {orig_name} — placeholder/stealth company")
                    continue

                vc_words = {"venture capital", "ventures", "capital", "investment", "angel", "fund", "vc "}
                if any(v in name_lower for v in vc_words):
                    print(f"    [skip] {orig_name} — VC/investment firm")
                    continue

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

                disqualifying_types = {
                    "incubator", "accelerator", "academy", "school",
                    "bootcamp", "boot camp", "university", "college",
                    "institute", "forum", "mixer", "show", "expo",
                    "conference", "summit", "meetup", "podcast",
                    "recruitment", "recruiting", "staffing", "headhunter",
                }
                if any(dt in name_lower for dt in disqualifying_types):
                    print(f"    [skip] {orig_name} — org type not a target company")
                    continue

                seen_companies.add(comp["slug"])
                companies.append({
                    "name": comp["name"],
                    "slug": comp["slug"],
                    "url": f"https://www.linkedin.com/company/{comp['slug']}/",
                })
                print(f"    + {comp['name']}")

            action_delay(config)

        except PlaywrightTimeout:
            print(f"  Timeout on page {page_num}, stopping pagination...")
            break
        except Exception as e:
            print(f"  Error on page {page_num}: {e}")
            break

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
    # No limit on finding — collect all, sort by priority, connect logic handles the cap
    # Ordered by priority — technical decision-makers first, then business roles
    role_patterns = [
        # Priority 1: Technical managers — most likely to hire contract devs
        "cto", "chief technology",
        "engineering manager", "tech lead", "technical lead",
        "head of engineering", "head of technology", "head of product",
        "vp of engineering", "vp of technology",
        "director of engineering", "director of technology",
        # Priority 2: Other C-level / VP
        "chief operating officer", "cmo", "cpo",
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
        # Mid-level managers that aren't decision makers for hiring
        "operations manager", "project manager", "project coordinator",
        "program manager", "delivery manager", "team leader",
        "scrum master", "agile coach",
        # Job seekers — they're looking for work, not hiring
        "available for", "looking for", "seeking", "open to work",
        "actively looking", "job search", "hire me",
        "#opentowork", "open_to_work",
    ]

    print(f"\n  Looking for decision-makers at {company['name']}...")

    # Visit the company's people page with keyword filter to pre-filter decision-makers
    dm_keywords = "manager, cto, ceo, founder, director, vp, head, president, chief"
    people_url = f"https://www.linkedin.com/company/{company['slug']}/people/?keywords={quote(dm_keywords)}"

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

        # Click "Show more results" up to 5 times to load more employees
        for click_num in range(1, 6):
            random_scroll(page)
            time.sleep(1)
            clicked = page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {
                        const text = btn.innerText.trim().toLowerCase();
                        if (text.includes('show more') || text.includes('load more') || text.includes('see more')) {
                            if (btn.offsetParent !== null) {
                                btn.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """)
            if not clicked:
                break
            print(f"    Loaded more results ({click_num})")
            time.sleep(2)

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

            # Exclude obvious non-targets by headline — saves unnecessary profile visits.
            # People whose headline is clearly just an IC / student / non-decision-maker.
            has_exclude = any(ex in headline_lower for ex in exclude_patterns)
            if has_exclude:
                # Allow through if headline also has a strong decision-maker signal
                strong_roles = ["founder", "co-founder", "ceo", "cto", "coo", "cmo", "cpo",
                                "chief", "head of", "vp ", "vice president", "managing director"]
                if not any(sr in headline_lower for sr in strong_roles):
                    continue

            # Don't filter by headline role — actual title comes from Experience section (checked in check_profile_activity)
            person = {
                "name": name,
                "headline": full_headline,
                "profile_url": profile_url,
                "company": company["name"],
                "company_url": company["url"],
                "matched_role": "",  # filled in by check_profile_activity via Experience section
                "likely_active": True,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
            }
            person["local"] = "yes" if local_mode else "no"
            people.append(person)
            seen_profiles.add(profile_url)
            print(f"    ? {name} — {full_headline} (will check experience)")

        action_delay(config)

    except PlaywrightTimeout:
        print(f"  Timeout looking at {company['name']}, moving on...")
    except Exception as e:
        print(f"  Error at {company['name']}: {e}")

    # Sort by role priority — CTOs and engineering managers first
    people.sort(key=lambda p: _role_priority.get(p.get("matched_role", ""), 99))
    # No cap here — let do_search decide how many to connect with

    return people


def check_profile_activity(page, person, config, local_mode=False):
    """Visit profile activity page — check for any 2+ activities in last 60 days."""
    try:
        # Visit profile first for connection degree
        page.goto(person["profile_url"], wait_until="domcontentloaded")
        page_delay(config)

        # Try to get connection degree
        degree_badge = page.query_selector('span.dist-value')
        if degree_badge:
            person["connection_degree"] = degree_badge.inner_text().strip()

        # Extract person's location from profile
        person_location = page.evaluate("""
            () => {
                // Strategy 1: Direct class match
                const locEl = document.querySelector('.text-body-small.inline.t-black--light.break-words');
                if (locEl) return locEl.innerText.trim();

                // Strategy 2: Look for location near "Contact info" link
                const contactLink = document.querySelector('a[href*="contact-info"]');
                if (contactLink) {
                    const parent = contactLink.closest('div');
                    if (parent) {
                        const prev = parent.previousElementSibling;
                        if (prev) return prev.innerText.trim();
                    }
                }

                // Strategy 3: Scan all small text spans in the top card area
                const topCard = document.querySelector('.pv-top-card, .scaffold-layout__main');
                const spans = (topCard || document).querySelectorAll('span');
                for (const s of spans) {
                    const text = s.innerText.trim();
                    // Location usually has a comma and place name
                    if (text.match(/^[A-Z].*,.*/) && text.length < 80 && !text.includes('|') && !text.includes('@')) {
                        return text;
                    }
                }

                // Strategy 4: Look for text containing "India", "United", etc near top
                const allEls = document.querySelectorAll('.text-body-small, .pv-text-details__left-panel span');
                for (const el of allEls) {
                    const text = el.innerText.trim();
                    if (text.length > 3 && text.length < 60 && text.includes(',')) {
                        return text;
                    }
                }
                return '';
            }
        """)
        if person_location:
            person["person_location"] = person_location
            print(f"    [location] {person['name']} is in {person_location}")

        # Check for "Open to work" badge — skip job seekers
        is_job_seeker = page.evaluate("""
            () => {
                const text = document.body.innerText.toLowerCase();
                // Check for open to work badge/banner
                if (text.includes('#opentowork') || text.includes('open to work')) {
                    // But make sure it's the profile badge, not in a post
                    const badge = document.querySelector('[class*="open-to-work"], [class*="opentowork"], .pv-top-card--open-to-work');
                    if (badge) return true;
                    // Also check the photo overlay
                    const photoFrame = document.querySelector('img[alt*="Open to work"], div[class*="open-to"]');
                    if (photoFrame) return true;
                }
                return false;
            }
        """)
        if is_job_seeker:
            print(f"    [skip] {person['name']} — has 'Open to work' badge, job seeker")
            person["has_recent_activity"] = False
            return person

        # Scroll down to trigger lazy-loading of the Experience section
        for _ in range(3):
            random_scroll(page)
            time.sleep(1)

        # Extract the job title at THIS specific company from the Experience section.
        # Strategy: find the company name in the experience text, then take the line above it
        # (LinkedIn layout: title line → company line → dates line → ...).
        # Also check for "Present" nearby to confirm it's a current role.
        company_name = person["company"]
        title_at_company = page.evaluate("""
            (companyName) => {
                const fullText = document.body.innerText;

                // Isolate the Experience section
                const expMatch = fullText.match(
                    /\\nExperience\\n([\\s\\S]*?)(?=\\n(?:Education|Skills|Certifications|Licenses|Recommendations|Volunteering|Interests|Languages|Publications|Projects|Honors|Courses|Organizations|Test scores)\\n|$)/
                );
                if (!expMatch) return null;

                const expText = expMatch[1];
                const lines = expText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

                // Build fuzzy match keywords from company name
                // e.g. "BeGig Technologies Pvt Ltd" → ["begig", "technologies"]
                const companyLower = companyName.toLowerCase();
                const companyWords = companyLower
                    .replace(/[^a-z0-9 ]/g, ' ')
                    .split(' ')
                    .filter(w => w.length > 3);  // skip short words like "pvt", "ltd", "the"

                for (let i = 1; i < lines.length; i++) {
                    const lineLower = lines[i].toLowerCase();

                    // Check if this line is the company line
                    const isMatch = companyWords.length > 0 &&
                        companyWords.some(w => lineLower.includes(w));
                    if (!isMatch) continue;

                    // Title is the line immediately above the company line
                    const title = lines[i - 1].toLowerCase();

                    // Peek ahead a few lines to check if this is a current role
                    const context = lines.slice(i, Math.min(lines.length, i + 4)).join(' ').toLowerCase();
                    const isCurrent = context.includes('present');

                    return { title, isCurrent, companyLine: lines[i] };
                }

                return null;
            }
        """, company_name)

        role_patterns_check = [
            "cto", "chief technology", "chief technical",
            "engineering manager", "tech lead", "technical lead",
            "head of engineering", "head of technology", "head of product",
            "vp of engineering", "vp of technology", "vp engineering",
            "director of engineering", "director of technology",
            "chief operating officer", "coo", "cmo", "cpo",
            "chief product", "chief marketing",
            "vp of", "vp ", "vice president", "managing director",
            "president",
            "ceo", "chief executive",
            "founder", "co-founder", "founder's office",
        ]

        matched_role = None
        matched_source = None
        headline = person.get("headline", "")

        # Determine the title to evaluate — prefer experience section, fall back to headline
        if title_at_company and title_at_company.get("title"):
            check_title = title_at_company["title"]
            is_current = title_at_company.get("isCurrent", False)
            company_line = title_at_company.get("companyLine", "")
            src_label = "exp (current)" if is_current else "exp (past)"
        else:
            check_title = headline
            company_line = company_name
            src_label = "headline"

        # Ask AI if this title = decision-maker who can assign contract work
        is_dm, role_label = _is_decision_maker_ai(check_title, company_name, headline)

        if is_dm is True:
            matched_role = role_label
            matched_source = f"ai+{src_label}"
        elif is_dm is False:
            print(f"    [skip] {person['name']} — AI: '{check_title}' is not a decision-maker ({role_label})")
            person["has_recent_activity"] = False
            return person
        else:
            # AI unavailable — fall back to string matching
            for role in role_patterns_check:
                if role in check_title.lower():
                    matched_role = role
                    matched_source = f"string+{src_label}"
                    break

        if matched_role:
            person["matched_role"] = matched_role
            print(f"    [{matched_source}] {person['name']} — '{check_title}' at '{company_line}'")
        else:
            print(f"    [skip] {person['name']} — no decision-maker role found at {company_name}")
            person["has_recent_activity"] = False
            return person

        # Visit "All activity" page — shows posts, comments, and reactions
        profile_slug = person["profile_url"].rstrip("/").split("/")[-1]
        activity_url = f"https://www.linkedin.com/in/{profile_slug}/recent-activity/all/"
        page.goto(activity_url, wait_until="domcontentloaded")
        time.sleep(3)
        random_scroll(page)
        time.sleep(2)

        # Count any activity — 60 days for both global and local
        activity_days = 60
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
    "connection_degree", "found_date", "connect_sent", "local",
]

_SALUTATIONS = {
    "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "dr", "dr.", "prof", "prof.",
    "sir", "shri", "smt", "ca", "er", "er.", "ca.",
}


def _is_decision_maker_ai(title, company, headline=""):
    """Ask Claude if this title can assign contract work. Returns (bool, role_label)."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        prompt = f"""Can this person assign or approve contract/freelance work for developers?

Title: {title}
Company: {company}
LinkedIn headline: {headline}

I'm looking for: CTOs, founders, engineering managers, heads of engineering, VPs, directors, CEOs, or anyone else with authority to hire contract developers.
I do NOT want: individual contributors, developers, designers, recruiters, interns, students, or mid-level non-hiring roles.

Reply with exactly two lines:
DECISION: YES or NO
ROLE: short label like "cto", "founder", "engineering manager", "vp engineering", "not a decision maker", etc."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        lines = {l.split(":")[0].strip().upper(): l.split(":", 1)[1].strip().lower()
                 for l in text.splitlines() if ":" in l}
        is_dm = lines.get("DECISION", "no") == "yes"
        role_label = lines.get("ROLE", title.lower()[:40])
        return is_dm, role_label
    except Exception as e:
        print(f"    [ai] Decision-maker check failed: {e}")
        return None, None  # None = unknown, fall back to string match


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

        person_location = person.get("person_location", "")

        local_context = ""
        if local_mode and location:
            # Chandigarh tricity area — all count as "local"
            nearby_cities = {"chandigarh", "mohali", "sas nagar", "panchkula", "zirakpur", "kharar", "derabassi", "baddi"}
            person_loc_lower = person_location.lower() if person_location else ""
            is_local = any(city in person_loc_lower for city in nearby_cities)

            if is_local:
                local_context = f"""
- I also live in {location} and work from home
- They are in: {person_location} (this is near {location}, same area)
- Mention we're from the same area casually
- Mention I'm open for contract work"""
            else:
                local_context = f"""
- I'm based in {location}, India, working from home
- They are located in: {person_location or 'unknown'}
- Do NOT say we're in the same city or 'fellow {location} person' — they're not local
- Just mention I'm a remote engineer open to contract work"""

        prompt = f"""Write a LinkedIn connection request note. MUST be under 300 characters total.

About me: Ankit, backend/DevOps engineer, 10+ years of experience, love learning new tech and solving problems, open to contract work.

About them:
- Name: {first_name}
- Found at company: {company}
- Role listed: {role}
- Their actual LinkedIn headline: {headline}
{local_context}

IMPORTANT: Check if their headline matches the company I found them at.
- If their headline mentions a DIFFERENT company or project, reference the headline company instead.
- Otherwise mention the company short name.

Rules:
- MUST be under 300 characters (hard LinkedIn limit)
- NO greeting like "Hi" or "Hey" — start directly with "I'm Ankit" or "Ankit here"
- Mention I'm a backend/DevOps engineer with 10+ years of experience
- Mention I love learning new tech and solving problems
- Mention I'm open to contract work
- Optionally mention their company name (short form only)
- No corporate buzzwords, no emojis
- End with "Let's connect!" or "Would love to connect!"
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
    company = person["company"].split(" - ")[0].split(" | ")[0].strip()
    if len(company) > 25:
        company = company[:22] + "..."

    if local_mode and location:
        msg = f"I'm Ankit, backend/DevOps engineer with 10+ yrs exp. Love solving problems and learning new tech. Based in {location}, open to contract work. Would love to connect!"
    else:
        msg = f"I'm Ankit, backend/DevOps engineer with 10+ yrs exp. Love learning new tech and solving problems. Saw {company} — open to contract work. Let's connect!"

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
        # Get formulas to extract URLs from HYPERLINK cells
        existing_formulas = sheet.get(value_render_option="FORMULA")

        # Ensure header exists
        if not existing_formulas:
            sheet.update(values=[FIELDNAMES], range_name="A1")
            existing_formulas = [FIELDNAMES]

        # Extract existing profile URLs from HYPERLINK formulas
        existing_urls = set()
        for row in existing_formulas[1:]:
            for cell in row:
                match = re.search(r'(https?://[^"]*linkedin\.com/in/[^"/?]+)', str(cell))
                if match:
                    existing_urls.add(match.group(1))

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
            next_row = len(existing_formulas) + 1
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
        if geo_id:
            config["_geo_id"] = geo_id
        print(f"\n--- LinkedIn Prospector [LOCAL: {location}] ---")
    else:
        print("\n--- LinkedIn Prospector ---")

    # Load existing data from Google Sheet
    existing_slugs, existing_profiles = _load_existing_from_sheet(config)
    config["_existing_slugs"] = existing_slugs

    print(f"Max companies: {config['max_companies_per_run']}")
    print(f"Max connects per company: {config.get('max_connects_per_company', 2)}")
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
                max_connects = config.get("max_connects_per_company", 2)
                connects_sent = 0
                for person in people:
                    if person["likely_active"]:
                        person = check_profile_activity(page, person, config, local_mode=local_mode)
                        # Regenerate message now that we have their actual location
                        person["message"] = generate_message(person, local_mode=local_mode, location=location)
                        # Step 4: Only send connect to active decision makers (up to max per company)
                        if auto_connect and person.get("has_recent_activity") and connects_sent < max_connects:
                            success = send_connection_request(page, person, config)
                            if success:
                                connects_sent += 1
                        page_delay(config)

            all_prospects.extend(people)

        # Save results
        if all_prospects:
            save_prospects(all_prospects, config)
            pass  # profiles tracked in Google Sheet
        else:
            print("\nNo matching prospects found this run. Try adjusting industry_codes or company_size_codes in config.json.")

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
