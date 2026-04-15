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
FILTER_STATE_FILE = SCRIPT_DIR / ".filter_matrix_state.json"

INDUSTRY_NAMES = {
    "4":    "Software Development",
    "96":   "IT Services & Consulting",
    "6":    "Tech, Info & Internet",
    "48":   "Computer & Network Security",
    "2458": "Data Infrastructure & Analytics",
    "5":    "Computer Networking",
    "3":    "Tech, Info & Media",
}
SIZE_LABELS = {"B": "1-10", "C": "11-50", "D": "51-200", "E": "201-500"}


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


def search_companies(page, config, force_combo=None):
    """Search LinkedIn for small tech companies using faceted filters (no keyword search).

    force_combo: dict from the filter matrix with geo_id, size, industry.
    When provided, skips the INDUSTRY_STATE_FILE rotation and uses the combo directly.
    """
    companies = []
    seen_companies = config.get("_existing_slugs", set())
    max_companies = config["max_companies_per_run"]

    if force_combo:
        # Matrix mode — use the combination's settings exactly
        current_industry = force_combo["industry"]
        size_codes = [force_combo["size"]]
        geo_id = force_combo.get("geo_id", "")
        industry_label = force_combo["industry_name"]
        next_label = "(matrix controls next)"
    else:
        # Legacy rotation mode — pick one industry per run from config
        industry_codes = config.get("industry_codes", ["96", "4"])
        size_codes = config.get("company_size_codes", ["C"])
        geo_id = config.get("_geo_id", "")

        if INDUSTRY_STATE_FILE.exists():
            with open(INDUSTRY_STATE_FILE) as f:
                rot_state = json.load(f)
            current_index = rot_state.get("last_index", 0)
        else:
            current_index = 0
        current_index = current_index % len(industry_codes)
        next_index = (current_index + 1) % len(industry_codes)
        current_industry = industry_codes[current_index]
        with open(INDUSTRY_STATE_FILE, "w") as f:
            json.dump({"last_index": next_index}, f)

        industry_label = INDUSTRY_NAMES.get(current_industry, f"industry {current_industry}")
        next_label = INDUSTRY_NAMES.get(industry_codes[next_index], f"industry {industry_codes[next_index]}")

    # Build faceted search URL
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

    if force_combo:
        geo_label = force_combo["geo_name"]
        size_label = force_combo["size_label"]
        print(f"\nSearching: {geo_label}  ·  {size_label} employees  ·  {industry_label}")
    else:
        print(f"\nSearching industry: {industry_label} (code {current_industry})")
        print(f"Next run will search: {next_label}")
    print(f"Search URL: {base_url}")

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

            # AI filter — drop non-tech companies (civil eng, military, NGOs, etc.)
            all_names = [c["name"] for c in page_companies]
            tech_names = _filter_tech_companies_ai(all_names)
            page_companies = [c for c in page_companies if c["name"] in tech_names]
            print(f"  Page {page_num}: {len(page_companies)} after AI tech filter")

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
        "e-learning", "sme", "academician", "instructor", "mentor", "coach",
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
    dm_keywords = "manager, cto, vp, head, president, chief, architect, senior, staff, principal, lead, developer, engineer"
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

        # Build candidate list from extracted people
        candidates = []
        for entry in raw_people:
            href = entry.get("href", "")
            match = re.search(r'/in/([^/?]+)', href)
            if not match:
                continue
            profile_url = f"https://www.linkedin.com/in/{match.group(1)}"
            if profile_url in seen_profiles:
                continue

            lines = entry.get("lines", [])
            name = lines[0] if lines else "Unknown"
            full_headline = " | ".join(lines[1:6]) if len(lines) > 1 else ""

            if not full_headline:
                continue

            candidates.append({
                "name": name,
                "headline": full_headline,
                "profile_url": profile_url,
            })

        print(f"    {len(candidates)} candidates")

        # If more than 10, ask AI to pick the best 10 to visit
        if len(candidates) > 10:
            print(f"    Asking AI to pick best 10 from {len(candidates)} candidates...")
            selected_indices = _pick_best_people_ai(candidates, company["name"])
            candidates = [c for i, c in enumerate(candidates) if i in selected_indices]
            print(f"    AI selected {len(candidates)} candidates to check")

        for candidate in candidates:
            person = {
                "name": candidate["name"],
                "headline": candidate["headline"],
                "profile_url": candidate["profile_url"],
                "company": company["name"],
                "company_url": company["url"],
                "matched_role": "",  # filled in by check_profile_activity
                "likely_active": True,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
            }
            person["local"] = "yes" if local_mode else "no"
            people.append(person)
            seen_profiles.add(candidate["profile_url"])
            print(f"    ? {candidate['name']} — {candidate['headline']}")

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

                // Helper: reject lines that are clearly not job titles
                function looksLikeTitle(line) {
                    const l = line.toLowerCase().trim();
                    if (l.length < 2 || l.length > 100) return false;
                    // Reject date patterns: "jan 2020", "2018 - 2022", "3 mos", "present"
                    if (/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b/.test(l)) return false;
                    if (/\b\d{4}\b/.test(l)) return false;
                    if (/\b(present|mos|yrs|yr\b|full.time|part.time|contract|freelance|self.employed)\b/.test(l)) return false;
                    // Reject pure location lines (city, country patterns)
                    if (/^[a-z ,.\-]+$/.test(l) && l.includes(',')) return false;
                    // Reject section headers
                    if (['experience', 'education', 'skills', 'about'].includes(l)) return false;
                    return true;
                }

                for (let i = 1; i < lines.length; i++) {
                    const lineLower = lines[i].toLowerCase();

                    // Check if this line is the company line
                    const isMatch = companyWords.length > 0 &&
                        companyWords.some(w => lineLower.includes(w));
                    if (!isMatch) continue;

                    // Scan backwards from company line to find a valid title line
                    let title = null;
                    for (let j = i - 1; j >= Math.max(0, i - 3); j--) {
                        if (looksLikeTitle(lines[j])) {
                            title = lines[j].toLowerCase();
                            break;
                        }
                    }
                    if (!title) continue;

                    // Peek ahead a few lines to check if current and employment type
                    const context = lines.slice(i, Math.min(lines.length, i + 5)).join(' ').toLowerCase();
                    const isCurrent = context.includes('present');
                    const isFreelance = context.includes('freelance') || context.includes('contract') || context.includes('self-employed');

                    return { title, isCurrent, isFreelance, companyLine: lines[i], expText: expText };
                }

                return { title: null, isCurrent: false, companyLine: null, expText: expText };
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
        exp_text = (title_at_company or {}).get("expText", "") or ""

        # Determine the title to evaluate — prefer experience section, fall back to headline
        if title_at_company and title_at_company.get("title"):
            is_current = title_at_company.get("isCurrent", False)
            is_freelance = title_at_company.get("isFreelance", False)
            company_line = title_at_company.get("companyLine", "")

            # Skip freelancers — they can't assign contract work
            if is_freelance:
                print(f"    [skip] {person['name']} — freelance/contract role at {company_name}, not a decision-maker")
                person["has_recent_activity"] = False
                return person

            # Only use the title if it's their CURRENT role — past titles are irrelevant
            if is_current:
                check_title = title_at_company["title"]
                src_label = "exp (current)"
            else:
                # Past role found — fall back to headline instead
                check_title = headline
                company_line = company_name
                src_label = "headline (exp was past role)"
        else:
            check_title = headline
            company_line = company_name
            src_label = "headline"

        # Ask AI: decision-maker, senior engineer, or skip?
        target_type, role_label = _is_decision_maker_ai(check_title, company_name, headline, exp_text)

        if target_type == "decision_maker":
            matched_role = role_label
            matched_source = f"ai+{src_label}"
            person["target_type"] = "decision_maker"
        elif target_type == "senior_engineer":
            matched_role = role_label
            matched_source = f"ai+{src_label}"
            person["target_type"] = "senior_engineer"
        elif target_type == "skip":
            print(f"    [skip] {person['name']} — AI: '{check_title}' → {role_label}")
            person["has_recent_activity"] = False
            return person
        else:
            # AI unavailable — fall back to string matching (decision-maker only)
            for role in role_patterns_check:
                if role in check_title.lower():
                    matched_role = role
                    matched_source = f"string+{src_label}"
                    person["target_type"] = "decision_maker"
                    break

        if matched_role:
            person["matched_role"] = matched_role
            print(f"    [{matched_source}] {person['name']} — '{check_title}' ({person.get('target_type', '?')})")
        else:
            print(f"    [skip] {person['name']} — no target role found at {company_name}")
            person["has_recent_activity"] = False
            return person

        # Navigate to the all-activity page — shows posts, comments AND reactions.
        activity_url = person["profile_url"].rstrip("/") + "/recent-activity/all/"
        page.goto(activity_url, wait_until="domcontentloaded")
        page_delay(config)
        # Scroll to load more activity items
        for _ in range(3):
            random_scroll(page)
            time.sleep(1)

        activity_count = page.evaluate("""
            () => {
                const threeMonthsMs = 90 * 24 * 60 * 60 * 1000;
                const cutoff = Date.now() - threeMonthsMs;
                let count = 0;

                // Strategy 1: <time datetime="..."> elements (absolute timestamps)
                const timeTags = document.querySelectorAll('time[datetime]');
                for (const t of timeTags) {
                    const dt = new Date(t.getAttribute('datetime'));
                    if (dt.getTime() > cutoff) count++;
                }

                // Strategy 2: relative time strings like "1w", "2mo" in aria-hidden spans
                if (count === 0) {
                    const spans = document.querySelectorAll('span[aria-hidden="true"]');
                    for (const s of spans) {
                        const text = s.innerText.trim().toLowerCase();
                        if (!text) continue;
                        // seconds/minutes/hours/days/weeks → within 3 months
                        if (text.match(/^\\d+\\s*(s|m|h|d|w)\\b/)) { count++; continue; }
                        // months: only if <= 3
                        const mo = text.match(/^(\\d+)\\s*mo\\b/);
                        if (mo && parseInt(mo[1]) <= 3) count++;
                    }
                }

                return count;
            }
        """)

        is_active = activity_count >= 1
        person["recent_activity_30d"] = activity_count
        person["has_recent_activity"] = is_active
        if is_active:
            print(f"    [active] {person['name']} — {activity_count} activities in last 3 months")
        else:
            print(f"    [inactive] {person['name']} — only {activity_count} activity in last 3 months, skipping connect")

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

FOLLOWUP_MESSAGE = (
    "Not sure if you're responsible for any hiring — just wanted to let you know "
    "I'm open to interesting contractual work. Let me know if something comes up. "
    "Have a nice day!"
)

_SALUTATIONS = {
    "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "dr", "dr.", "prof", "prof.",
    "sir", "shri", "smt", "ca", "er", "er.", "ca.",
}


def _filter_tech_companies_ai(company_names):
    """Ask AI which company names look like tech/software companies that might hire
    a backend/DevOps contractor. Returns a set of names to keep.
    Falls back to keeping all if API unavailable."""
    if not company_names:
        return set(company_names)
    try:
        import anthropic
        client = anthropic.Anthropic()
        numbered = "\n".join(f"{i+1}. {name}" for i, name in enumerate(company_names))
        prompt = f"""I'm looking for SMALL tech/software companies (11–50 employees) that might hire a contract backend or DevOps engineer.

Which of these company names are likely small tech or software companies?
Include: small software companies, SaaS startups, IT services, cloud/DevOps firms, AI/ML startups, fintech, cybersecurity, dev tools — likely 11–50 employees.
Exclude:
- Civil engineering, construction, military, associations, NGOs, roofing, government bodies, non-tech trade groups.
- Well-known large enterprises (Fortune 500, globally recognised brands with thousands of employees) — e.g. Accenture, Nvidia, Google, Microsoft, Amazon, IBM, Infosys, Wipro, TCS, Cognizant, Capgemini, Anthropic, Meta, Apple, Oracle, SAP, Salesforce, etc.

{numbered}

Reply with just the numbers of companies to KEEP, comma-separated. Example: 1, 3, 5
Only numbers, nothing else."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        keep_indices = set()
        for part in re.split(r'[,\s]+', text):
            part = part.strip().rstrip('.')
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(company_names):
                    keep_indices.add(company_names[idx])
        return keep_indices if keep_indices else set(company_names)
    except Exception as e:
        print(f"  [ai] Company filter failed: {e}")
        return set(company_names)


def _pick_best_people_ai(candidates, company):
    """Given a list of {name, headline} dicts, ask AI to pick the best 10 to visit.
    Returns a set of indices to keep. Falls back to first 10 if API unavailable."""
    try:
        import anthropic
        client = anthropic.Anthropic()

        numbered = "\n".join(
            f"{i+1}. {c['name']} — {c['headline']}"
            for i, c in enumerate(candidates)
        )
        prompt = f"""I found these people at a company called "{company}". I need to visit their profiles to check their experience and activity, but I can only check 10.

Pick the best 10 people most likely to be either:
- A decision-maker who can assign contract developer work (CTO, VP Eng, Engineering Manager, Tech Lead, Architect, etc.)
- A senior backend/DevOps/cloud engineer with likely 8+ years of experience

{numbered}

Reply with just the numbers of your top 10 picks, comma-separated. Example: 1, 3, 5, 7, 8, 9, 11, 14, 17, 20
Only numbers, nothing else."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        selected = set()
        for part in re.split(r'[,\s]+', text):
            part = part.strip().rstrip('.')
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(candidates):
                    selected.add(idx)
        if selected:
            return selected
    except Exception as e:
        print(f"    [ai] People selection failed: {e}")

    # Fallback: first 10
    return set(range(min(10, len(candidates))))


def _is_decision_maker_ai(title, company, headline="", exp_text=""):
    """Ask Claude if this person is worth connecting with.
    Returns (target_type, role_label) where target_type is:
      'decision_maker' — can assign contract work
      'senior_engineer' — senior backend/DevOps engineer, 8+ years
      'skip'           — not a useful contact
      None             — API unavailable, fall back to string match
    """
    try:
        import anthropic
        client = anthropic.Anthropic()
        prompt = f"""Evaluate this LinkedIn profile to decide if I should send a connection request.

Current title: {title}
Company: {company}
LinkedIn headline: {headline}
Experience section (for calculating total years):
{exp_text[:600] if exp_text else "(not available)"}

I want to connect with TWO types of people:

TYPE 1 — Decision-maker: someone who can assign or approve contract/freelance developer work.
Examples: CTO, Engineering Manager, VP Engineering, Head of Engineering, Tech Lead, Solutions Architect, Tech Architect, President, Chief Officer.
NOT: recruiters, HR, designers, interns, sales, mid-level ops, mentors, coaches, trainers, educators, teachers.

TYPE 2 — Senior backend/DevOps engineer with 8+ years of total experience.
These are peers who may be working on personal projects or have side opportunities.
Estimate total years from the experience section dates. Only qualify if clearly 8+ years in backend, DevOps, cloud, or infrastructure roles.
NOT: junior/mid engineers, QA, frontend-only, data science.

Reply with exactly two lines:
TYPE: DECISION_MAKER or SENIOR_ENGINEER or SKIP
ROLE: short label like "cto", "engineering manager", "senior devops engineer", "skip - recruiter", etc."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        parsed = {l.split(":")[0].strip().upper(): l.split(":", 1)[1].strip().lower()
                  for l in text.splitlines() if ":" in l}
        target_type_raw = parsed.get("TYPE", "skip")
        role_label = parsed.get("ROLE", title.lower()[:40])

        if "decision_maker" in target_type_raw:
            return "decision_maker", role_label
        elif "senior_engineer" in target_type_raw:
            return "senior_engineer", role_label
        else:
            return "skip", role_label
    except Exception as e:
        print(f"    [ai] Decision-maker check failed: {e}")
        return None, None  # None = API unavailable, fall back to string match


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

        target_type = person.get("target_type", "decision_maker")

        if target_type == "senior_engineer":
            prompt = f"""Write a LinkedIn connection request note. MUST be under 300 characters total.

About me: Ankit, backend/DevOps engineer, 10+ years of experience, love learning new tech and solving hard problems.

About them: {first_name}, a senior backend/DevOps engineer at {company}.
Their headline: {headline}

Rules:
- MUST be under 300 characters (hard LinkedIn limit)
- Start with "{first_name}, you seem to be doing interesting work at [short company name]"
- Peer-to-peer tone — we're both senior engineers
- Say I'm Ankit, backend/DevOps engineer, happy to help with backlogs or short-term contract work
- End with "let's connect anyway!" or "anyway, let's connect!"
- No "Hi" or "Hey", no emojis, no corporate buzzwords
- Just output the message, nothing else"""
        else:
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
- Start with "{first_name}, you seem to be doing interesting work at [short company name]"
- Then: "I'm Ankit, backend/DevOps engineer — happy to help with any backlogs or short-term contract work"
- End with "let's connect anyway!" or "anyway, let's connect!"
- No "Hi" or "Hey", no emojis, no corporate buzzwords
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

    first_name = _clean_first_name(person.get("name", ""))
    target_type = person.get("target_type", "decision_maker")
    if target_type == "senior_engineer":
        msg = f"{first_name}, you seem to be doing interesting work at {company}. I'm Ankit, backend/DevOps engineer — happy to help with backlogs or short-term contract work. Anyway, let's connect!"
    elif local_mode and location:
        msg = f"{first_name}, you seem to be doing interesting work at {company}. I'm Ankit, backend/DevOps engineer based in {location} — happy to help with any contract work. Anyway, let's connect!"
    else:
        msg = f"{first_name}, you seem to be doing interesting work at {company}. I'm Ankit, backend/DevOps engineer — happy to help with backlogs or short-term contract work. Anyway, let's connect!"

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


def _generate_reply_ai(their_message, person_name="", our_original=""):
    """Generate a short, natural reply to someone who responded to our connection note."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        first_name = _clean_first_name(person_name) if person_name else "there"
        original_context = f'\nMy original connection note to them: "{our_original}"' if our_original else ""
        prompt = f"""Someone replied to my LinkedIn connection request. Write a short, warm reply.

My context: I'm Ankit, backend/DevOps engineer, 10+ years exp, open to contract work.{original_context}
Their message: "{their_message}"

Rules:
- Under 200 characters total
- Match the casual, genuine tone of my original note
- If they said thanks / nice to meet you → acknowledge warmly and say I'm open to help if they ever need contract work
- If they asked a question → answer naturally
- No greeting words like "Hi" or "Hey", no emojis
- Output just the message text, nothing else"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        msg = response.content[0].text.strip().strip('"')
        if len(msg) > 200:
            msg = msg[:197] + "..."
        return msg
    except Exception as e:
        print(f"    [ai] Reply generation failed: {e}")
        return "Thanks for connecting! Happy to chat if you ever need backend or DevOps contract work."


def _generate_followup_ai(thread_messages=None, our_note="", person_name=""):
    """Generate a contextual follow-up based on the full conversation thread.

    If they previously showed interest (mentioned having work, projects, engagements)
    → friendly reminder about what they mentioned.
    If they never replied or showed no interest
    → light nudge: not sure if you're hiring, open to contract work, have a nice day.
    """
    try:
        import anthropic
        client = anthropic.Anthropic()

        # Build thread text for Claude
        if thread_messages:
            thread_lines = []
            for msg in thread_messages:
                sender_label = "Ankit (me)" if msg["sender"] == "us" else (person_name or "them")
                thread_lines.append(f"{sender_label}: {msg['text']}")
            thread_text = "\n".join(thread_lines)
        elif our_note:
            thread_text = f"Ankit (me): {our_note}\n(no reply received)"
        else:
            thread_text = "(no conversation history available)"

        prompt = f"""Write a short LinkedIn follow-up message based on this conversation.

Full conversation so far:
{thread_text}

My context: I'm Ankit, backend/DevOps engineer, 10+ years exp, open to contract work.

Instructions:
- Read the conversation carefully.
- If they mentioned having work, projects, or potential engagements → write a friendly reminder like "just a gentle reminder about the [work/projects] you mentioned — still very much interested"
- If they never replied or showed no real interest → write a light nudge: "not sure if you're responsible for any hiring, just wanted to let you know I'm open to interesting contractual work, let me know if something comes up, have a nice day"
- Match the casual tone already established in the conversation
- Under 200 characters
- No "Hi" or "Hey" opener, no emojis, no corporate buzzwords
- Output just the message text, nothing else"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        msg = response.content[0].text.strip().strip('"')
        if len(msg) > 200:
            msg = msg[:197] + "..."
        return msg
    except Exception as e:
        print(f"    [ai] Follow-up generation failed: {e}")
        return FOLLOWUP_MESSAGE


def send_message_in_conversation(page, message):
    """Type and send a message in the currently open LinkedIn conversation."""
    # LinkedIn uses a contenteditable div, not a textarea
    msg_box = (
        page.query_selector('.msg-form__contenteditable[contenteditable="true"]')
        or page.query_selector('div.msg-form__contenteditable')
        or page.query_selector('div[role="textbox"][contenteditable="true"]')
        or page.query_selector('div[data-placeholder*="message" i][contenteditable]')
    )
    if not msg_box:
        print("    [inbox] Could not find message input box")
        return False

    msg_box.click()
    time.sleep(0.5)
    page.keyboard.press("Control+a")
    page.keyboard.type(message)
    time.sleep(1)

    sent = page.evaluate("""
        () => {
            const btns = document.querySelectorAll('button');
            for (const btn of btns) {
                const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                const text = btn.innerText.trim().toLowerCase();
                if ((text === 'send' || aria === 'send' || aria.includes('send message'))
                        && btn.offsetParent !== null && !btn.disabled) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }
    """)
    if sent:
        time.sleep(2)
    return bool(sent)


def _scan_conversations(page):
    """Extract conversations from the LinkedIn messaging inbox.

    Uses the confirmed DOM selectors from LinkedIn's current UI:
    - Container: ul.msg-conversations-container__conversations-list
    - Items: li.msg-conversation-listitem
    - Name: h3.msg-conversation-listitem__participant-names span.truncate
    - Preview: p.msg-conversation-card__message-snippet
    - Unread: t-bold class on the preview <p>

    LinkedIn always prefixes logged-in user's messages with "You:" in the preview.
    Returns a list of dicts: idx, name, preview, isUnread, lastSenderIsUs, ourNote.
    """
    return page.evaluate("""
        () => {
            const list = document.querySelector(
                '.msg-conversations-container__conversations-list'
            );
            if (!list) return [];

            const items = list.querySelectorAll('li.msg-conversation-listitem');
            const results = [];

            for (let i = 0; i < items.length; i++) {
                const li = items[i];

                // ── Name ──────────────────────────────────────────────
                let name = 'Unknown';
                const h3 = li.querySelector('h3.msg-conversation-listitem__participant-names');
                if (h3) {
                    const span = h3.querySelector('span.truncate');
                    name = (span || h3).innerText.trim().split('\\n')[0].trim();
                }

                // ── Preview ────────────────────────────────────────────
                const previewEl = li.querySelector('p.msg-conversation-card__message-snippet');
                const preview = previewEl ? previewEl.innerText.trim() : '';

                // ── Unread: LinkedIn bolds the preview <p> for unread msgs ──
                const isUnread = previewEl
                    ? previewEl.classList.toString().includes('t-bold')
                    : li.classList.toString().includes('unread') ||
                      !!li.querySelector('[class*="unread"]');

                // ── Who sent last ──────────────────────────────────────
                // "You: ..." means we sent last; anything else means they did
                let lastSenderIsUs = null;
                if (preview.toLowerCase().startsWith('you:') ||
                    preview.toLowerCase().startsWith('you ')) {
                    lastSenderIsUs = true;
                } else if (preview && !preview.toLowerCase().startsWith('sponsored')) {
                    lastSenderIsUs = false;
                }

                // ── Our original note (strip "You: " prefix) ───────────
                let ourNote = '';
                if (lastSenderIsUs && preview) {
                    ourNote = preview.replace(/^you:\s*/i, '').trim();
                }

                results.push({ idx: i, name, preview, isUnread, lastSenderIsUs, ourNote });
            }
            return results;
        }
    """) or []


def _open_conversation_by_idx(page, idx):
    """Scroll the conversation at idx into view, click it, and wait for the thread to load."""
    clicked = page.evaluate("""
        (idx) => {
            const list = document.querySelector(
                '.msg-conversations-container__conversations-list'
            );
            if (!list) return false;
            const items = list.querySelectorAll('li.msg-conversation-listitem');
            if (idx >= items.length) return false;
            const li = items[idx];
            li.scrollIntoView({ block: 'nearest' });
            const linkDiv = li.querySelector('.msg-conversation-listitem__link');
            if (linkDiv) { linkDiv.click(); return true; }
            li.click();
            return true;
        }
    """, idx)
    if clicked:
        time.sleep(3)  # wait for thread panel to load
    return bool(clicked)


def _get_full_thread(page, person_name=""):
    """Read the full conversation from the open thread panel.
    Returns a list of {sender: 'us'|'them', text: str} dicts."""
    raw = page.evaluate("""
        () => {
            const msgs = [];
            const items = document.querySelectorAll('.msg-s-event-listitem');
            for (const item of items) {
                const body = item.querySelector('.msg-s-event-listitem__body');
                if (!body) continue;
                const text = body.innerText.trim();
                if (!text || text.length < 2) continue;
                // Their messages have a profile avatar; ours don't
                const hasAvatar = !!item.querySelector(
                    'img.presence-entity__image, .msg-s-event-listitem__icon img'
                );
                msgs.push({ sender: hasAvatar ? 'them' : 'us', text });
            }
            return msgs;
        }
    """) or []
    return raw


def do_inbox(playwright, config, do_replies=True, do_followup=False):
    """Open LinkedIn messaging, reply to conversations where they replied,
    and/or send follow-up messages to conversations where we haven't heard back."""
    session_file = SESSION_DIR / "state.json"
    if not session_file.exists():
        print("No saved session found. Run with --login first.")
        sys.exit(1)

    modes = []
    if do_replies:
        modes.append("REPLY")
    if do_followup:
        modes.append("FOLLOWUP")
    print(f"\n--- LinkedIn Inbox [{' + '.join(modes)}] ---")

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

    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        time.sleep(3)
        if "login" in page.url:
            print("Session expired. Run with --login to re-authenticate.")
            browser.close()
            sys.exit(1)

        page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded")
        time.sleep(4)

        try:
            page.wait_for_selector(
                '.msg-conversations-container__conversations-list', timeout=10000
            )
        except PlaywrightTimeout:
            print("Could not find conversation list. Check your session.")
            debug_snapshot(page, "inbox_load_fail")
            browser.close()
            return

        # Scroll the conversation list panel (not the page) to trigger lazy-loading
        # of all conversation items. LinkedIn uses virtual scroll so items below
        # the fold won't render until scrolled into view.
        print("Scrolling conversation list to load all items...")
        for _ in range(6):
            page.evaluate("""
                () => {
                    // Find the scrollable panel that contains the conversation list
                    const list = document.querySelector(
                        '.msg-conversations-container__conversations-list'
                    );
                    const panel = list && (
                        list.closest('.msg-conversations-container') ||
                        list.closest('[class*="conversations-container"]') ||
                        list.parentElement
                    );
                    if (panel) panel.scrollTop += 600;
                    else if (list) list.scrollTop += 600;
                }
            """)
            time.sleep(1)
        # Scroll back to top so indices are consistent during iteration
        page.evaluate("""
            () => {
                const list = document.querySelector(
                    '.msg-conversations-container__conversations-list'
                );
                const panel = list && (
                    list.closest('.msg-conversations-container') ||
                    list.parentElement
                );
                if (panel) panel.scrollTop = 0;
                else if (list) list.scrollTop = 0;
            }
        """)
        time.sleep(1)

        conversations = _scan_conversations(page)
        print(f"Found {len(conversations)} conversations in inbox\n")

        if not conversations:
            print("No conversations parsed — saving debug snapshot.")
            debug_snapshot(page, "inbox_empty_parse")
            browser.close()
            return

        # Print a summary upfront so you can see what was detected
        for conv in conversations:
            sender_tag = "last=US  " if conv.get("lastSenderIsUs") else \
                         "last=THEM" if conv.get("lastSenderIsUs") is False else "last=?   "
            unread_tag = "[UNREAD]" if conv.get("isUnread") else "        "
            print(f"  {unread_tag} {sender_tag}  {conv['name']}: {conv.get('preview','')[:70]}")

        print()
        replied_count = 0
        followup_count = 0

        for conv in conversations:
            idx = conv["idx"]
            name = conv["name"]
            is_unread = conv.get("isUnread", False)
            preview = conv.get("preview", "")
            last_sender_is_us = conv.get("lastSenderIsUs")
            # ourNote is the preview text stripped of "You:" — our original connection note
            our_note = conv.get("ourNote", "")

            should_reply = do_replies and is_unread and last_sender_is_us is False
            should_followup = do_followup and last_sender_is_us is True

            if not should_reply and not should_followup:
                continue

            # Fast pre-filter: if the preview already looks like a follow-up message we sent,
            # the thread definitely has 2+ messages → no need to open it.
            _followup_signals = [
                "contractual", "not sure if you", "just wanted to let you know",
                "if anything comes up", "if anything interesting", "no worries if you",
                "gentle reminder", "humble reminder",
            ]
            if should_followup and any(s in preview.lower() for s in _followup_signals):
                print(f"  [followup] {name} — preview shows follow-up already sent, skipping")
                continue

            # Click the conversation to open it in the right panel
            print(f"\n  Opening: {name}")
            if not _open_conversation_by_idx(page, idx):
                print(f"  [inbox] Could not click conversation for {name}, skipping")
                continue

            # Read the full thread so AI has full context
            thread = _get_full_thread(page, person_name=name)
            if thread:
                print(f"    Thread ({len(thread)} messages):")
                for m in thread:
                    who = "me" if m["sender"] == "us" else name
                    print(f"      {who}: {m['text'][:70]}")
            else:
                print(f"    (thread not loaded yet, using preview as context)")

            if should_reply:
                # Find last message from them in the thread
                their_msg = next(
                    (m["text"] for m in reversed(thread) if m["sender"] == "them"), preview
                )
                print(f"    Their message: {their_msg[:80]}")
                our_original = next(
                    (m["text"] for m in thread if m["sender"] == "us"), our_note
                )
                reply_text = _generate_reply_ai(their_msg, name, our_original=our_original)
                print(f"  [reply] Sending: {reply_text}")
                if send_message_in_conversation(page, reply_text):
                    print(f"  [reply] SENT to {name}")
                    replied_count += 1
                else:
                    print(f"  [reply] Could not send to {name}")

            elif should_followup:
                # Only follow up when there is exactly 1 message and it's ours.
                # Any back-and-forth (replies, multi-message threads) → skip.
                if not thread:
                    print(f"  [followup] {name} — could not read thread, skipping to be safe")
                    continue
                if not (len(thread) == 1 and thread[0]["sender"] == "us"):
                    print(f"  [followup] {name} — thread has {len(thread)} message(s), skipping")
                    continue
                followup_text = _generate_followup_ai(
                    thread_messages=thread,
                    our_note=our_note,
                    person_name=name,
                )
                print(f"  [followup] Sending: {followup_text}")
                if send_message_in_conversation(page, followup_text):
                    print(f"  [followup] SENT to {name}")
                    followup_count += 1
                else:
                    print(f"  [followup] Could not send to {name}")

            action_delay(config)

        print(f"\nDone. Replies sent: {replied_count} | Follow-ups sent: {followup_count}")
        context.storage_state(path=str(session_file))

    except KeyboardInterrupt:
        print("\nInterrupted!")
    except Exception as e:
        print(f"\nError in inbox: {e}")
    finally:
        try:
            browser.close()
        except Exception:
            pass


def _generate_matrix_combinations(config):
    """Generate all filter combinations in priority order from config's filter_matrix."""
    matrix = config.get("filter_matrix", {})
    geos = matrix.get("geos", [])
    sizes = matrix.get("sizes", ["C"])
    industries = matrix.get("industries", list(INDUSTRY_NAMES.keys()))

    combos = []
    for geo in geos:
        for industry in industries:
            for size in sizes:
                geo_slug = geo["name"].replace(", ", "_").replace(" ", "_")
                combos.append({
                    "combo_id": f"{geo_slug}_{size}_{industry}",
                    "geo_name": geo["name"],
                    "geo_id": geo.get("id", ""),
                    "size": size,
                    "size_label": SIZE_LABELS.get(size, size),
                    "industry": industry,
                    "industry_name": INDUSTRY_NAMES.get(industry, industry),
                    "status": "pending",   # pending | in_progress | exhausted
                    "companies_found": 0,
                    "runs": 0,
                    "last_run": None,
                })
    return combos


def load_filter_state(config):
    """Load filter matrix state, or initialise it from config if missing."""
    if FILTER_STATE_FILE.exists():
        with open(FILTER_STATE_FILE) as f:
            state = json.load(f)
        # If new geos/industries were added to config, append missing combos
        existing_ids = {c["combo_id"] for c in state["combinations"]}
        for combo in _generate_matrix_combinations(config):
            if combo["combo_id"] not in existing_ids:
                state["combinations"].append(combo)
                print(f"  [matrix] New combination added: {combo['geo_name']} / {combo['industry_name']}")
        return state

    # First run — create fresh state
    combos = _generate_matrix_combinations(config)
    state = {"combinations": combos}
    save_filter_state(state)
    print(f"  [matrix] Initialised filter matrix with {len(combos)} combinations.")
    return state


def save_filter_state(state):
    with open(FILTER_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_next_combination(state):
    """Return (index, combo) for the first pending or in_progress combination."""
    for i, combo in enumerate(state["combinations"]):
        if combo["status"] in ("pending", "in_progress"):
            return i, combo
    return None, None   # all exhausted


def print_filter_stats(state):
    """Print a progress table of all filter combinations."""
    combos = state["combinations"]
    exhausted = sum(1 for c in combos if c["status"] == "exhausted")
    in_prog   = sum(1 for c in combos if c["status"] == "in_progress")
    pending   = sum(1 for c in combos if c["status"] == "pending")
    total = len(combos)

    idx_next, next_combo = get_next_combination(state)

    bar_done  = "█" * exhausted
    bar_left  = "░" * (total - exhausted)
    pct = int(exhausted / total * 100) if total else 0

    print(f"\n{'═'*72}")
    print(f" Filter Matrix — {exhausted}/{total} done ({pct}%) · {in_prog} in progress · {pending} pending")
    print(f" [{bar_done}{bar_left}]")
    print(f"{'═'*72}")
    print(f"  {'':2} {'Geo':<22} {'Size':<7} {'Industry':<26} {'Status':<12} {'Runs':>5} {'Found':>6}")
    print(f"  {'─'*2} {'─'*22} {'─'*7} {'─'*26} {'─'*12} {'─'*5} {'─'*6}")

    # Group by geo for readability
    current_geo = None
    for i, combo in enumerate(combos):
        if combo["geo_name"] != current_geo:
            current_geo = combo["geo_name"]

        status = combo["status"]
        icon = "✓" if status == "exhausted" else \
               "▶" if status == "in_progress" else "·"
        marker = "►► " if i == idx_next else "   "

        print(
            f"{marker}{icon} "
            f"{combo['geo_name']:<22} "
            f"{combo['size_label']:<7} "
            f"{combo['industry_name']:<26} "
            f"{status:<12} "
            f"{combo['runs']:>5} "
            f"{combo['companies_found']:>6}"
        )

    print(f"{'═'*72}")
    if next_combo:
        print(f" Next run: {next_combo['geo_name']}  ·  {next_combo['size_label']} employees  ·  {next_combo['industry_name']}")
    else:
        print(" All combinations exhausted. Reset .filter_matrix_state.json to restart.")
    print()


def do_search(playwright, config, auto_connect=False, local_mode=False):
    """Run the full search pipeline."""
    session_file = SESSION_DIR / "state.json"
    if not session_file.exists():
        print("No saved session found. Run with --login first.")
        sys.exit(1)

    location = ""
    force_combo = None
    filter_state = None

    if local_mode:
        # Local mode uses its own geo — bypass matrix
        local_config = config.get("local_mode", {})
        location = local_config.get("location", "")
        geo_id = local_config.get("geo_id", "")
        config = {**config}
        if geo_id:
            config["_geo_id"] = geo_id
        print(f"\n--- LinkedIn Prospector [LOCAL: {location}] ---")
    elif config.get("filter_matrix", {}).get("geos"):
        # Matrix mode — pick next combination
        filter_state = load_filter_state(config)
        combo_idx, force_combo = get_next_combination(filter_state)
        if force_combo is None:
            print("\nAll filter combinations exhausted!")
            print_filter_stats(filter_state)
            return
        force_combo["status"] = "in_progress"
        force_combo["runs"] += 1
        force_combo["last_run"] = datetime.now().strftime("%Y-%m-%d")
        save_filter_state(filter_state)
        print(f"\n--- LinkedIn Prospector [MATRIX] ---")
        print(f"Combination {combo_idx + 1}/{len(filter_state['combinations'])}: "
              f"{force_combo['geo_name']} · {force_combo['size_label']} emp · {force_combo['industry_name']}")
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

        # Step 1: Find companies (pass force_combo for matrix mode)
        companies = search_companies(page, config, force_combo=force_combo)

        # Update matrix state based on search result
        if filter_state and force_combo is not None:
            force_combo["companies_found"] += len(companies)
            if len(companies) == 0:
                force_combo["status"] = "exhausted"
                print(f"\n[matrix] No new companies found — marking combination exhausted.")
                # Preview next
                _, nxt = get_next_combination(filter_state)
                if nxt:
                    print(f"[matrix] Next combination: {nxt['geo_name']} · {nxt['industry_name']}")
            save_filter_state(filter_state)
            print_filter_stats(filter_state)

        # Step 2: Find people at each company
        total = len(companies)
        for company_idx, company in enumerate(companies, start=1):
            print(f"\n── Company {company_idx}/{total}: {company['name']} ──")
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
        else:
            print("\nNo matching prospects found this run.")

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
    parser.add_argument("--login",    action="store_true", help="Open browser for manual LinkedIn login")
    parser.add_argument("--search",   action="store_true", help="Run the company/people search (default if no flags)")
    parser.add_argument("--connect",  action="store_true", help="Auto-send connection requests with personalized notes")
    parser.add_argument("--local",    action="store_true", help="Chandigarh mode — target local companies with local messaging")
    parser.add_argument("--inbox",    action="store_true", help="Reply to people who responded to your connection requests")
    parser.add_argument("--followup", action="store_true", help="Send follow-up message to people who haven't replied yet")
    parser.add_argument("--matrix",   action="store_true", help="Show filter matrix progress table and exit (no search)")
    args = parser.parse_args()

    # Default to search if no flags given
    if not args.login and not args.search and not args.connect and not args.local \
            and not args.inbox and not args.followup and not args.matrix:
        args.search = True

    # --connect or --local implies --search
    if args.connect or args.local:
        args.search = True

    config = load_config()
    SESSION_DIR.mkdir(exist_ok=True)

    if args.matrix:
        state = load_filter_state(config)
        print_filter_stats(state)
        return

    with sync_playwright() as p:
        if args.login:
            do_login(p)
        if args.search:
            do_search(p, config, auto_connect=args.connect, local_mode=args.local)
        if args.inbox or args.followup:
            do_inbox(p, config, do_replies=args.inbox, do_followup=args.followup)


if __name__ == "__main__":
    main()
