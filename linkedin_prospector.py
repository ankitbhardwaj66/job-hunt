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

        for size_code in ["B", "C"]:
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

                for comp in page_companies:
                    if len(companies) >= max_companies:
                        break
                    if comp["slug"] in seen_companies:
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
    role_patterns = ["founder", "co-founder", "ceo", "cto", "coo", "chief",
                     "head of", "vp ", "vice president", "director", "lead",
                     "engineering manager", "tech lead"]

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
            # Look for headline in the text lines
            headline = ""
            for line in lines[1:6]:
                line_lower = line.lower()
                for role in target_roles + role_patterns:
                    if role in line_lower:
                        headline = line
                        break
                if headline:
                    break

            # If no headline found from lines, check all text
            if not headline:
                matched = False
                for role in target_roles + role_patterns:
                    if role in text:
                        matched = True
                        # Try to find the line containing the role
                        for line in lines[1:]:
                            if role in line.lower():
                                headline = line
                                break
                        if not headline:
                            headline = role.title()
                        break
                if not matched:
                    continue

            headline_lower = headline.lower()
            matched_role = None
            for role in target_roles + role_patterns:
                if role in headline_lower:
                    matched_role = role
                    break

            if not matched_role:
                continue

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
    """Visit a profile to check if they're recently active on LinkedIn."""
    try:
        page.goto(person["profile_url"], wait_until="domcontentloaded")
        page_delay(config)
        random_scroll(page)

        # Check for recent activity section
        activity_section = page.query_selector('section.artdeco-card a[href*="/recent-activity/"]')
        if activity_section:
            person["has_recent_activity"] = True
        else:
            person["has_recent_activity"] = False

        # Try to get connection degree
        degree_badge = page.query_selector('span.dist-value')
        if degree_badge:
            person["connection_degree"] = degree_badge.inner_text().strip()

    except Exception:
        person["has_recent_activity"] = None

    return person


def save_prospects(prospects, config):
    """Save prospects to CSV file."""
    output_file = SCRIPT_DIR / config["output_file"]
    file_exists = output_file.exists()

    fieldnames = [
        "name", "headline", "profile_url", "company", "company_url",
        "matched_role", "likely_active", "has_recent_activity",
        "connection_degree", "found_date",
    ]

    with open(output_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for person in prospects:
            writer.writerow(person)

    print(f"\nSaved {len(prospects)} prospects to {output_file}")


def do_search(playwright, config):
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

            # Step 3: Optionally check profile activity (slower, more risky)
            for person in people:
                # Only check top prospects to limit page visits
                if person["likely_active"]:
                    person = check_profile_activity(page, person, config)
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
    args = parser.parse_args()

    # Default to search if no flags given
    if not args.login and not args.search:
        args.search = True

    config = load_config()
    SESSION_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        if args.login:
            do_login(p)
        if args.search:
            do_search(p, config)


if __name__ == "__main__":
    main()
