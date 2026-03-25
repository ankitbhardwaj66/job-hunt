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
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SESSION_DIR = SCRIPT_DIR / ".linkedin_session"
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


def search_companies(page, config):
    """Search LinkedIn for small tech companies and return company info."""
    companies = []
    seen_companies = set()
    max_companies = config["max_companies_per_run"]
    keywords = config["search_keywords"]

    # Shuffle keywords so each run explores different searches
    random.shuffle(keywords)

    for keyword in keywords:
        if len(companies) >= max_companies:
            break

        print(f"\nSearching for: {keyword}")
        search_url = f"https://www.linkedin.com/search/results/companies/?keywords={quote(keyword)}&companySize=%5B%22B%22%5D"
        # companySize B = 1-10, C = 11-50. We search B first.
        # We'll also try C in a second pass.

        for size_code in ["%22B%22", "%22C%22"]:
            if len(companies) >= max_companies:
                break

            url = f"https://www.linkedin.com/search/results/companies/?keywords={quote(keyword)}&companySize=%5B{size_code}%5D"

            try:
                page.goto(url, wait_until="domcontentloaded")
                page_delay(config)
                random_scroll(page)

                # Get company cards from search results
                company_links = page.query_selector_all('a.app-aware-link[href*="/company/"]')

                for link in company_links:
                    if len(companies) >= max_companies:
                        break

                    href = link.get_attribute("href")
                    if not href or "/company/" not in href:
                        continue

                    # Extract company slug
                    parts = href.split("/company/")
                    if len(parts) < 2:
                        continue
                    slug = parts[1].strip("/").split("?")[0].split("/")[0]

                    if slug in seen_companies or not slug:
                        continue
                    seen_companies.add(slug)

                    # Try to get company name from the link text
                    name_el = link.query_selector("span[dir='ltr'] > span[aria-hidden='true']")
                    company_name = name_el.inner_text().strip() if name_el else slug

                    companies.append({
                        "name": company_name,
                        "slug": slug,
                        "url": f"https://www.linkedin.com/company/{slug}/",
                        "keyword": keyword,
                    })
                    print(f"  Found: {company_name}")
                    action_delay(config)

            except PlaywrightTimeout:
                print(f"  Timeout searching for {keyword}, moving on...")
            except Exception as e:
                print(f"  Error searching for {keyword}: {e}")

    print(f"\nFound {len(companies)} companies total.")
    return companies


def find_people_at_company(page, company, config, seen_profiles):
    """Find decision-makers at a given company."""
    people = []
    target_roles = [r.lower() for r in config["target_roles"]]
    max_people = config["max_people_per_company"]

    print(f"\n  Looking for decision-makers at {company['name']}...")

    # Search for people at this company
    people_url = f"https://www.linkedin.com/search/results/people/?currentCompany=%5B%22{company['slug']}%22%5D&keywords={quote(company['name'])}"

    try:
        page.goto(people_url, wait_until="domcontentloaded")
        page_delay(config)
        random_scroll(page)

        # Get people results
        result_items = page.query_selector_all('li.reusable-search__result-container')

        for item in result_items:
            if len(people) >= max_people:
                break

            try:
                # Get profile link
                profile_link = item.query_selector('a.app-aware-link[href*="/in/"]')
                if not profile_link:
                    continue

                profile_url = profile_link.get_attribute("href")
                if not profile_url:
                    continue
                profile_url = profile_url.split("?")[0]

                if profile_url in seen_profiles:
                    continue

                # Get name
                name_el = item.query_selector('span[dir="ltr"] > span[aria-hidden="true"]')
                name = name_el.inner_text().strip() if name_el else "Unknown"

                # Get headline/title
                headline_el = item.query_selector('div.entity-result__primary-subtitle')
                headline = headline_el.inner_text().strip() if headline_el else ""

                # Check if this person has a target role
                headline_lower = headline.lower()
                matched_role = None
                for role in target_roles:
                    if role in headline_lower:
                        matched_role = role
                        break

                # Also check for common patterns
                if not matched_role:
                    role_patterns = ["founder", "ceo", "cto", "chief", "head of", "vp ", "director", "lead"]
                    for pattern in role_patterns:
                        if pattern in headline_lower:
                            matched_role = pattern
                            break

                if not matched_role:
                    continue

                # Check if they seem active (has recent activity indicator)
                # We can't always tell from search, but having a headline is a good sign
                is_likely_active = len(headline) > 10

                person = {
                    "name": name,
                    "headline": headline,
                    "profile_url": profile_url,
                    "company": company["name"],
                    "company_url": company["url"],
                    "matched_role": matched_role,
                    "likely_active": is_likely_active,
                    "found_date": datetime.now().strftime("%Y-%m-%d"),
                }
                people.append(person)
                seen_profiles.add(profile_url)
                print(f"    Found: {name} — {headline}")

            except Exception as e:
                continue

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
