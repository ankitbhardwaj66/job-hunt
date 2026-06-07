#!/usr/bin/env python3
"""
LinkedIn Freelance Post Outreach
Searches LinkedIn posts for freelance/part-time developer opportunities
and sends tailored DMs.

Usage:
    python linkedin_freelance_outreach.py --login     # save session first
    python linkedin_freelance_outreach.py             # run outreach
"""

import argparse
import json
import os
import random
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

SCRIPT_DIR = Path(__file__).parent
SESSION_DIR = SCRIPT_DIR / ".linkedin_session"
STATE_FILE = SCRIPT_DIR / ".freelance_outreach_state.json"

CV_LINK = "https://docs.google.com/document/d/1cRz4A1vvtUP6_uzm4xD7EQ0iOEyihsgjXfBlpdA7Ajk/edit?usp=sharing"

SEARCH_KEYWORDS = [
    "hiring freelance developer",
    "freelance devops engineer",
    "freelance backend developer",
    "freelance part-time software developer",
    "looking for freelance developer",
    "contract developer hiring",
    "remote freelance developer",
    "freelance python developer",
    "freelance cloud engineer",
    "freelance infrastructure engineer",
    "contract devops engineer",
    "side project developer",
]

# Words that indicate full-time job posts — skip these
FULLTIME_SIGNALS = [
    "full-time", "full time", "permanent", "joining immediately",
    "notice period", "ctc", "lpa", "annual salary", "permanent position",
    "looking for a full", "full time opportunity",
]

# Role classification keywords
DEVOPS_KEYWORDS = ["devops", "aws", "kubernetes", "docker", "ci/cd", "terraform", "cloud", "infrastructure", "sre"]
BACKEND_KEYWORDS = ["backend", "back-end", "python", "node", "api", "server", "django", "flask", "rest"]
FRONTEND_KEYWORDS = ["frontend", "front-end", "react", "angular", "vue", "javascript", "html", "css"]

MESSAGES = {
    "devops_backend": (
        "Hi {name}, came across your post about freelance opportunities. "
        "I'm currently employed full-time as a Senior SRE, and I'm available for "
        "contract/freelance work on the side. 12+ years across DevOps (AWS, Kubernetes CKAD, "
        "Terraform, CI/CD) and Python backend (REST APIs, Flask, microservices). "
        "Happy to take on project-based work. Resume: {cv}"
    ),
    "devops": (
        "Hi {name}, came across your post about freelance DevOps work. "
        "I'm a Senior SRE/DevOps Engineer — AWS certified, Kubernetes CKAD, Terraform certified — "
        "available for contract/project work alongside my current full-time role. "
        "Resume: {cv}"
    ),
    "backend": (
        "Hi {name}, came across your post about freelance backend work. "
        "I'm a Senior Engineer with 12+ years in Python backend — REST APIs, Flask, "
        "microservices, AWS, PostgreSQL/MySQL. Available for contract/project work "
        "alongside my full-time role. Resume: {cv}"
    ),
    "frontend": (
        "Hi {name}, came across your post about freelance opportunities. "
        "My core expertise is backend (Python, APIs) and DevOps (AWS, Kubernetes, Terraform), "
        "with light frontend capability. Available for contract/project work "
        "alongside my full-time role. Resume: {cv}"
    ),
    "general": (
        "Hi {name}, came across your post about freelance developer opportunities. "
        "I'm a Senior DevOps/Backend Engineer — Python, AWS, Kubernetes, Terraform, CI/CD — "
        "available for contract/project work alongside my current full-time role. "
        "Resume: {cv}"
    ),
}


def human_delay(min_sec=2, max_sec=5):
    time.sleep(random.uniform(min_sec, max_sec))


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"contacted": [], "skipped": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def classify_role(post_text):
    text = post_text.lower()
    has_devops = any(k in text for k in DEVOPS_KEYWORDS)
    has_backend = any(k in text for k in BACKEND_KEYWORDS)
    has_frontend = any(k in text for k in FRONTEND_KEYWORDS)

    if has_devops and has_backend:
        return "devops_backend"
    elif has_devops:
        return "devops"
    elif has_backend:
        return "backend"
    elif has_frontend:
        return "frontend"
    return "general"


def is_relevant_post(text):
    text_lower = text.lower()

    # Must mention freelance/contract/part-time work
    has_freelance = any(k in text_lower for k in [
        "freelance", "part-time", "part time", "contract", "project-based",
        "side project", "gig", "short-term", "project work",
    ])
    # Must mention a relevant role
    has_role = any(k in text_lower for k in [
        "developer", "devops", "engineer", "backend", "frontend", "software", "cloud",
    ])
    # Skip full-time job posts
    is_fulltime = any(k in text_lower for k in FULLTIME_SIGNALS)

    return has_freelance and has_role and not is_fulltime


def extract_name_from_text(text):
    """Extract poster name from post card text."""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    # Skip known header lines
    skip = {"feed post", "like", "comment", "repost", "send", "follow", "• 1st", "• 2nd", "• 3rd+", "• 3rd", "promoted"}
    for line in lines:
        if line.lower() in skip:
            continue
        if line.startswith("•") or line.startswith("·"):
            continue
        if len(line) > 2 and len(line) < 60 and not any(c in line for c in ["@", "http", "/"]):
            return line.split()[0]  # First word = first name
    return "there"


def build_message(role, name):
    first_name = name.split()[0] if name and name != "there" else "there"
    template = MESSAGES.get(role, MESSAGES["general"])
    return template.format(name=first_name, cv=CV_LINK)


def login(playwright):
    browser = playwright.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page.goto("https://www.linkedin.com/login")
    print("Log in to LinkedIn manually, then press Enter...")
    input()
    context.storage_state(path=str(SESSION_DIR / "state.json"))
    browser.close()
    print("Session saved.")


def search_posts(page, keyword):
    url = f"https://www.linkedin.com/search/results/content/?keywords={quote(keyword)}&origin=GLOBAL_SEARCH_HEADER"
    page.goto(url, wait_until="domcontentloaded")
    human_delay(4, 6)

    # Scroll to load more posts
    for _ in range(5):
        page.keyboard.press("End")
        human_delay(2, 3)

    # Save debug snapshot on first call to help identify selectors
    debug_dir = SCRIPT_DIR / "debug"
    debug_dir.mkdir(exist_ok=True)
    snapshot_path = debug_dir / f"search_{keyword[:20].replace(' ','_')}.png"
    html_path = debug_dir / f"search_{keyword[:20].replace(' ','_')}.html"
    page.screenshot(path=str(snapshot_path), full_page=True)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page.content())
    print(f"  Debug snapshot: {snapshot_path}")

    posts = []
    try:
        # Diagnostic — understand page state
        diag = page.evaluate("""() => {
            const all = document.querySelectorAll('a');
            const inLinks = Array.from(all).filter(l => l.href && l.href.includes('/in/'));
            const hasLike = document.body.innerText.includes('Like');
            const hasComment = document.body.innerText.includes('Comment');
            return {
                totalLinks: all.length,
                inProfileLinks: inLinks.length,
                sampleLinks: inLinks.slice(0, 3).map(l => l.href),
                bodyTextLength: document.body.innerText.length,
                hasLike,
                hasComment,
            };
        }""")
        print(f"  Diag: {diag}")

        # Use JS — find posts by working backwards from profile /in/ links
        results = page.evaluate("""() => {
            const posts = [];
            const seen = new Set();

            const profileLinks = Array.from(document.querySelectorAll('a[href*="/in/"]'));

            for (const link of profileLinks) {
                const href = link.href ? link.href.split('?')[0] : '';
                if (!href || seen.has(href)) continue;

                // Walk up DOM to find post card container
                let container = link.parentElement;
                for (let i = 0; i < 12; i++) {
                    if (!container) break;
                    const text = container.innerText || '';
                    // Post card has substantial text + action buttons
                    if (text.length > 80 && (
                        text.includes('Like') || text.includes('Comment') ||
                        text.includes('Repost') || text.includes('Send')
                    )) {
                        seen.add(href);
                        // Get poster name
                        const nameEl = link.querySelector('span[aria-hidden="true"]');
                        const name = nameEl
                            ? nameEl.innerText.trim().split('\\n')[0]
                            : (link.innerText.trim().split('\\n')[0] || 'there');

                        // Also capture any embedded job links in this card
                        const jobLinks = Array.from(container.querySelectorAll('a[href*="/jobs/view/"]'))
                            .map(l => l.href.split('?')[0]);

                        posts.push({
                            profile_url: href,
                            name: name,
                            text: text.substring(0, 800),
                            job_links: jobLinks,
                        });
                        break;
                    }
                    container = container.parentElement;
                }
            }
            return posts;
        }""")

        print(f"  Found {len(results)} raw cards")

        for r in results:
            if is_relevant_post(r.get("text", "")):
                posts.append({
                    "name": r.get("name", "there"),
                    "text": r.get("text", ""),
                    "profile_url": r.get("profile_url", ""),
                    "job_links": r.get("job_links", []),
                })

    except Exception as e:
        print(f"  Error parsing posts: {e}")

    return posts


def send_dm(page, profile_url, message, state):
    """Navigate to profile page and send DM via Message button (not comment box)."""
    if "/in/" not in profile_url:
        return False
    profile_id = profile_url.rstrip("/").split("/in/")[-1].split("/")[0]
    clean_url = f"https://www.linkedin.com/in/{profile_id}/"

    if profile_id in state["contacted"]:
        print(f"  Already contacted: {profile_id}")
        return False
    if profile_id in state["skipped"]:
        print(f"  Previously skipped: {profile_id}")
        return False

    # Navigate to the profile page directly
    page.goto(clean_url, wait_until="domcontentloaded")
    human_delay(3, 4)

    try:
        # Click Message button — only inside profile header, NOT comment sections
        clicked = page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            for (const btn of buttons) {
                const text = (btn.innerText || '').trim();
                const label = btn.getAttribute('aria-label') || '';
                if ((text === 'Message' || label.toLowerCase() === 'message') &&
                    !btn.closest('.comments-comment-box') &&
                    !btn.closest('.feed-shared-update-v2') &&
                    !btn.closest('.social-actions')) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")

        if not clicked:
            print("  No Message button on profile — skipping")
            state["skipped"].append(profile_id)
            return False

        human_delay(2, 3)

        # Check for InMail credits exhausted / upgrade popup
        upgrade_popup = page.query_selector("text='Message with Premium', text='Upgrade my plan'")
        if not upgrade_popup:
            upgrade_popup = page.evaluate("""() => {
                return document.body.innerText.includes('Upgrade my plan') ||
                       document.body.innerText.includes('InMail credits');
            }""")
        if upgrade_popup:
            print("  ⚠️  InMail credits exhausted — stopping script")
            # Close popup
            page.keyboard.press("Escape")
            raise KeyboardInterrupt

        # Wait for DM overlay (not a comment box)
        dm_box = None
        for sel in [
            ".msg-form__contenteditable",
            ".msg-overlay-conversation-bubble div[contenteditable='true']",
            ".msg-content-container div[contenteditable='true']",
        ]:
            try:
                dm_box = page.wait_for_selector(sel, timeout=6000)
                if dm_box:
                    break
            except PlaywrightTimeout:
                continue

        if not dm_box:
            print("  DM overlay did not open — skipping")
            state["skipped"].append(profile_id)
            return False

        # Fill subject if field exists
        subject = "Available for Freelance DevOps / Backend Work"
        try:
            subj_box = page.query_selector("input[placeholder*='Subject'], input[placeholder*='subject']")
            if subj_box:
                subj_box.click()
                subj_box.type(subject, delay=20)
                human_delay(0.5, 1)
        except Exception:
            pass

        # Re-find message body after subject (dm_box ref may be stale)
        dm_box = None
        for sel in [
            ".msg-form__contenteditable",
            ".msg-overlay-conversation-bubble div[contenteditable='true']",
            "div[contenteditable='true'][aria-label*='message' i]",
            "div[contenteditable='true']",
        ]:
            try:
                dm_box = page.wait_for_selector(sel, timeout=4000)
                if dm_box:
                    break
            except PlaywrightTimeout:
                continue

        if not dm_box:
            print("  Message body not found after subject")
            state["skipped"].append(profile_id)
            return False

        dm_box.click()
        human_delay(0.3, 0.5)
        dm_box.type(message, delay=25)
        human_delay(1, 2)

        print(f"  → Sending to {profile_id}...")

        # Re-focus the message box and use Ctrl+Enter (LinkedIn send shortcut)
        dm_box.click()
        human_delay(0.5, 1)
        page.keyboard.press("Control+Enter")
        human_delay(2, 3)

        # Verify it was sent — message box should be empty now
        still_has_text = page.evaluate("""() => {
            const box = document.querySelector(
                '.msg-form__contenteditable, .msg-overlay-conversation-bubble div[contenteditable="true"]'
            );
            return box ? (box.innerText || '').trim().length > 0 : false;
        }""")

        if not still_has_text:
            state["contacted"].append(profile_id)
            print(f"  ✓ Sent to {profile_id}")
            return True
        else:
            # Fallback: click the blue send icon button by aria-label or position
            clicked = page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const sendBtn = btns.find(b => {
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    return label.includes('send') && !b.closest('.comments-comment-box');
                });
                if (sendBtn) { sendBtn.click(); return true; }
                return false;
            }""")
            human_delay(2, 3)
            if clicked:
                state["contacted"].append(profile_id)
                print(f"  ✓ Sent to {profile_id}")
                return True
            else:
                print("  Could not send — skipping")
                state["skipped"].append(profile_id)

    except PlaywrightTimeout:
        print("  Timeout on profile page")
        state["skipped"].append(profile_id)

    return False


def run_outreach(playwright):
    state_path = SESSION_DIR / "state.json"
    if not state_path.exists():
        print("No session found. Run with --login first.")
        return

    browser = playwright.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        storage_state=str(state_path),
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800},
    )
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page = context.new_page()
    state = load_state()

    try:
        # Verify logged in
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        human_delay(2, 3)
        if "login" in page.url:
            print("Session expired. Run with --login first.")
            return

        all_posts = []
        seen_urls = set()

        for keyword in SEARCH_KEYWORDS:
            print(f"\nSearching: '{keyword}'")
            posts = search_posts(page, keyword)
            for post in posts:
                url = post["profile_url"]
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_posts.append(post)
            print(f"  Found {len(posts)} relevant posts")
            human_delay(3, 6)

        # Remove already contacted/skipped
        all_posts = [
            p for p in all_posts
            if p["profile_url"].rstrip("/").split("/in/")[-1] not in state["contacted"]
            and p["profile_url"].rstrip("/").split("/in/")[-1] not in state["skipped"]
        ]

        print(f"\nTotal new prospects: {len(all_posts)}")

        for i, post in enumerate(all_posts):
            role = classify_role(post["text"])
            name = post["name"] if post["name"] != "there" else extract_name_from_text(post["text"])
            message = build_message(role, name)

            print(f"\n[{i+1}/{len(all_posts)}] {name} — {role.upper()} — {post['profile_url'].rstrip('/').split('/')[-1]}")
            print(f"  Post: {post['text'][:120].strip()}...")

            sent = send_dm(page, post["profile_url"], message, state)
            save_state(state)
            if sent:
                human_delay(20, 35)  # Pause between sends to avoid detection

        print(f"\nDone. Contacted: {len(state['contacted'])} | Skipped: {len(state['skipped'])}")

        # Print all job links found in posts
        all_job_links = []
        for post in all_posts:
            for link in post.get("job_links", []):
                if link not in all_job_links:
                    all_job_links.append(link)

        if all_job_links:
            print(f"\n{'='*60}")
            print(f"JOB LINKS FOUND IN POSTS ({len(all_job_links)} total):")
            print('='*60)
            for link in all_job_links:
                print(f"  {link}")
            print('='*60)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        save_state(state)
        context.close()
        browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="Save LinkedIn session")
    args = parser.parse_args()

    SESSION_DIR.mkdir(exist_ok=True)

    with sync_playwright() as playwright:
        if args.login:
            login(playwright)
        else:
            run_outreach(playwright)


if __name__ == "__main__":
    main()
