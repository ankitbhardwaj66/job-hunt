#!/usr/bin/env python3
"""Debug script — test sending a connection request to a specific profile.

Usage: python debug_connect.py <profile_url>
Example: python debug_connect.py https://www.linkedin.com/in/johndoe
"""

import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).parent
SESSION_DIR = SCRIPT_DIR / ".linkedin_session"
DEBUG_DIR = SCRIPT_DIR / "debug"

MESSAGE = "Hey! Stumbled on your profile and thought it looked really cool. I'm a dev who works with small teams — backend, cloud, DevOps stuff. Would be great to connect!"


def main():
    if len(sys.argv) < 2:
        print("Usage: python debug_connect.py <linkedin_profile_url>")
        print("Example: python debug_connect.py https://www.linkedin.com/in/johndoe")
        sys.exit(1)

    profile_url = sys.argv[1]
    DEBUG_DIR.mkdir(exist_ok=True)
    session_file = SESSION_DIR / "state.json"

    # Clear old debug screenshots
    for f in DEBUG_DIR.glob("debug_*.png"):
        f.unlink()

    with sync_playwright() as p:
        browser = p.chromium.launch(
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

        # Step 1: Go to profile
        print(f"\n[1] Navigating to profile: {profile_url}")
        page.goto(profile_url, wait_until="domcontentloaded")
        time.sleep(5)

        page.screenshot(path=str(DEBUG_DIR / "debug_01_profile.png"), full_page=False)
        print(f"    Current URL: {page.url}")
        print(f"    Screenshot: debug/debug_01_profile.png")

        if "404" in page.url or "404" in page.title():
            print("    ERROR: Profile not found (404)")
            browser.close()
            sys.exit(1)

        # Step 2: List all visible buttons
        buttons = page.evaluate("""
            () => {
                const btns = document.querySelectorAll('button');
                return Array.from(btns).map(btn => ({
                    text: btn.innerText.trim().substring(0, 80),
                    ariaLabel: btn.getAttribute('aria-label') || '',
                    visible: btn.offsetParent !== null,
                })).filter(b => b.visible && b.text.length > 0);
            }
        """)
        print(f"\n[2] Visible buttons on page ({len(buttons)}):")
        for i, btn in enumerate(buttons):
            marker = " <<< CONNECT" if btn["text"] == "Connect" else ""
            if "connect" in btn["ariaLabel"].lower():
                marker = f" <<< aria: {btn['ariaLabel']}"
            print(f"    [{i}] '{btn['text']}' (aria: '{btn['ariaLabel']}'){marker}")

        # Step 3: Click Connect
        print(f"\n[3] Clicking Connect button...")
        connect_result = page.evaluate("""
            () => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.innerText.trim();
                    const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if ((text === 'Connect' || ariaLabel.includes('invite') || ariaLabel.includes('connect'))
                        && !ariaLabel.includes('message')
                        && btn.offsetParent !== null) {
                        const rect = btn.getBoundingClientRect();
                        return {
                            found: true, text: text, ariaLabel: ariaLabel,
                            x: Math.round(rect.x), y: Math.round(rect.y),
                            width: Math.round(rect.width), height: Math.round(rect.height)
                        };
                    }
                }
                return { found: false };
            }
        """)
        print(f"    Button found: {connect_result}")

        if not connect_result["found"]:
            print("\n[3b] No Connect button found, trying More dropdown...")
            more_btn = page.query_selector('button[aria-label="More actions"], button:has-text("More")')
            if more_btn and more_btn.is_visible():
                more_btn.click()
                time.sleep(1.5)
                page.screenshot(path=str(DEBUG_DIR / "debug_03b_dropdown.png"), full_page=False)
                items = page.evaluate("""
                    () => {
                        const items = document.querySelectorAll('[role="listbox"] li, .artdeco-dropdown__content li');
                        return Array.from(items).map(i => i.innerText.trim().substring(0, 50));
                    }
                """)
                print(f"    Dropdown items: {items}")
                print(f"    Screenshot: debug/debug_03b_dropdown.png")
                # Don't click — just report
                page.keyboard.press("Escape")
            else:
                print("    No More button either")

            print("\n--- DRY RUN COMPLETE (no Connect button found) ---")
            browser.close()
            return

        # Actually click it now
        page.evaluate("""
            () => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.innerText.trim();
                    const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if ((text === 'Connect' || ariaLabel.includes('invite') || ariaLabel.includes('connect'))
                        && !ariaLabel.includes('message')
                        && btn.offsetParent !== null) {
                        btn.click();
                        return;
                    }
                }
            }
        """)

        print("    Clicked! Waiting 4s for modal...")
        time.sleep(4)

        # Step 4: Screenshot after click
        page.screenshot(path=str(DEBUG_DIR / "debug_04_after_connect.png"), full_page=False)
        print(f"\n[4] After Connect click")
        print(f"    URL: {page.url}")
        print(f"    Screenshot: debug/debug_04_after_connect.png")

        # Check for modal
        modal_info = page.evaluate("""
            () => {
                const modals = document.querySelectorAll('div[role="dialog"], .artdeco-modal, .artdeco-modal__content');
                if (modals.length === 0) return { hasModal: false };
                const modal = modals[modals.length - 1];
                const btns = modal.querySelectorAll('button');
                const textareas = modal.querySelectorAll('textarea');
                return {
                    hasModal: true,
                    modalText: modal.innerText.trim().substring(0, 300),
                    buttons: Array.from(btns).map(b => b.innerText.trim()).filter(t => t.length > 0),
                    textareas: textareas.length,
                };
            }
        """)
        print(f"    Modal info: {modal_info}")

        # Check all visible buttons now
        all_btns = page.evaluate("""
            () => {
                return Array.from(document.querySelectorAll('button'))
                    .filter(b => b.offsetParent !== null && b.innerText.trim())
                    .map(b => b.innerText.trim().substring(0, 50));
            }
        """)
        print(f"    All visible buttons now: {all_btns}")

        # Check if "Pending" appeared (request already sent without modal)
        pending = any("pending" in b.lower() for b in all_btns)
        if pending:
            print("\n    WARNING: 'Pending' button appeared — request was sent WITHOUT a modal!")
            page.screenshot(path=str(DEBUG_DIR / "debug_04b_pending.png"), full_page=False)

        # Step 5: Try "Add a note"
        if modal_info.get("hasModal"):
            print(f"\n[5] Looking for 'Add a note'...")
            has_add_note = any("add" in b.lower() and "note" in b.lower() for b in modal_info.get("buttons", []))
            print(f"    'Add a note' in modal buttons: {has_add_note}")

            if has_add_note:
                page.evaluate("""
                    () => {
                        const buttons = document.querySelectorAll('button');
                        for (const btn of buttons) {
                            if (btn.innerText.trim().toLowerCase().includes('add a note') && btn.offsetParent !== null) {
                                btn.click();
                                return;
                            }
                        }
                    }
                """)
                time.sleep(2)
                page.screenshot(path=str(DEBUG_DIR / "debug_05_add_note.png"), full_page=False)
                print(f"    Clicked 'Add a note'. Screenshot: debug/debug_05_add_note.png")

                # Find textareas
                textareas = page.evaluate("""
                    () => {
                        return Array.from(document.querySelectorAll('textarea')).map(t => ({
                            name: t.name, id: t.id, placeholder: t.placeholder,
                            visible: t.offsetParent !== null
                        }));
                    }
                """)
                print(f"    Textareas: {textareas}")

                # Type message (but DON'T send)
                note_field = page.query_selector('div[role="dialog"] textarea, .artdeco-modal textarea, textarea')
                if note_field:
                    note_field.fill(MESSAGE)
                    time.sleep(1)
                    page.screenshot(path=str(DEBUG_DIR / "debug_06_typed.png"), full_page=False)
                    print(f"\n[6] Message typed. Screenshot: debug/debug_06_typed.png")
                    print(f"    NOT sending — this is a dry run. Press Escape to cancel.")
                else:
                    print("    NO TEXTAREA FOUND!")

        # Cancel everything
        print(f"\n[7] Cancelling...")
        page.keyboard.press("Escape")
        time.sleep(1)
        page.keyboard.press("Escape")
        time.sleep(1)

        page.screenshot(path=str(DEBUG_DIR / "debug_07_final.png"), full_page=False)
        print(f"    Final state. Screenshot: debug/debug_07_final.png")
        print(f"\n--- DEBUG COMPLETE ---")

        try:
            browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
