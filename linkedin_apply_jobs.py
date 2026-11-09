import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


SEARCH_URL = (
    "https://www.linkedin.com/jobs/search-results/"
    "?currentJobId=4440401536&keywords=.net%20react%20remote"
    "&origin=JOB_SEARCH_PAGE_JOB_FILTER"
    "&referralSearchId=mMZsIFYI9OPt11Ouag93KA%3D%3D"
    "&geoId=90000608&f_TPR=r604800&f_AL=true"
)
CHROME_PROFILE_DIR = (
    Path(os.environ["LOCALAPPDATA"]) / "LinkedInJobSearch" / "ChromeProfile"
    if os.environ.get("LOCALAPPDATA")
    else None
)
STATE_FILE = Path(__file__).parent / ".linkedin-application-state.json"
ENV_FILE = Path(__file__).parent / ".env"
EMAIL_FIELD_SELECTORS = (
    "#session_key",
    'input[name="session_key"]',
    'input[data-tracking-control-name="seo-authwall-base_sign-in-session-key"]',
    'input[autocomplete="username"]',
    "#username",
)
PASSWORD_FIELD_SELECTORS = (
    "#session_password",
    'input[name="session_password"]',
    'input[data-tracking-control-name="seo-authwall-base_sign-in-password"]',
    'input[autocomplete="current-password"]',
    "#password",
)
JOB_ID_PATTERN = re.compile(r"/jobs/view/[^/?#]*?(\d{7,})(?:[/?#]|$)", re.IGNORECASE)
SUCCESS_PATTERN = re.compile(
    r"application (?:was )?(?:successfully )?(?:submitted|sent)|"
    r"your application has been sent|application submitted",
    re.IGNORECASE,
)
load_dotenv(ENV_FILE)


class UnsupportedApplicationQuestions(RuntimeError):
    pass


class UnrecognizedApplicationStep(RuntimeError):
    pass


def visible_locator(page, selectors):
    for selector in selectors:
        candidates = page.locator(selector)
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if candidate.is_visible():
                return candidate
    return None


def wait_for_visible_locator(page, selectors, label, timeout_ms=20_000):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        candidate = visible_locator(page, selectors)
        if candidate is not None:
            return candidate
        if page.is_closed():
            raise RuntimeError(f"LinkedIn closed before the {label} appeared.")
        time.sleep(0.25)
    raise RuntimeError(
        f"Could not find a visible LinkedIn {label}. "
        "Check that the runner opened the Sign in form in its Chrome window."
    )


def read_state():
    if not STATE_FILE.exists():
        return {}
    with STATE_FILE.open(encoding="utf-8") as state_file:
        state = json.load(state_file)
    if not isinstance(state, dict):
        raise ValueError(f"{STATE_FILE.name} must contain a JSON object.")
    return state


def write_state(state):
    with STATE_FILE.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2)
        state_file.write("\n")


def add_visible_cursor(context):
    context.add_init_script(
        """(() => {
            const install = () => {
                if (document.getElementById("linkedin-runner-cursor")) return;
                const cursor = document.createElement("div");
                cursor.id = "linkedin-runner-cursor";
                cursor.setAttribute("aria-hidden", "true");
                Object.assign(cursor.style, {
                    position: "fixed",
                    left: "0",
                    top: "0",
                    width: "28px",
                    height: "28px",
                    border: "3px solid #e11d48",
                    borderRadius: "50%",
                    background: "rgba(225, 29, 72, .24)",
                    boxShadow: "0 0 0 2px #fff, 0 1px 8px rgba(0,0,0,.7)",
                    pointerEvents: "none",
                    zIndex: "2147483647",
                    transform: "translate(-50%, -50%)",
                    display: "none"
                });
                document.documentElement.appendChild(cursor);
                document.addEventListener("mousemove", event => {
                    cursor.style.display = "block";
                    cursor.style.left = `${event.clientX}px`;
                    cursor.style.top = `${event.clientY}px`;
                }, true);
                document.addEventListener("mousedown", () => {
                    cursor.style.background = "rgba(250, 204, 21, .7)";
                }, true);
                document.addEventListener("mouseup", () => {
                    cursor.style.background = "rgba(225, 29, 72, .24)";
                }, true);
            };
            if (document.documentElement) install();
            else document.addEventListener("DOMContentLoaded", install, { once: true });
        })();"""
    )


def click_with_visible_cursor(page, locator, description):
    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()
    if box is None:
        raise RuntimeError(f"Could not locate {description} on screen.")
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    print(f"Moving visible cursor to {description} at ({round(x)}, {round(y)}).", flush=True)
    page.mouse.move(x, y, steps=12)
    time.sleep(0.35)
    locator.click(timeout=10_000)


def center_locator_in_scroll_container(locator, description):
    visibility = locator.evaluate(
        """element => {
            const scrollAncestors = [];
            for (let ancestor = element.parentElement; ancestor;
                 ancestor = ancestor.parentElement) {
                if (ancestor.scrollHeight > ancestor.clientHeight + 1) {
                    scrollAncestors.push(ancestor);
                }
            }

            const scrollPositions = [];
            for (const ancestor of scrollAncestors) {
                const before = ancestor.scrollTop;
                ancestor.scrollTop = ancestor.scrollHeight;
                scrollPositions.push({
                    before,
                    after: ancestor.scrollTop,
                    max: ancestor.scrollHeight - ancestor.clientHeight
                });
            }

            element.scrollIntoView({
                block: "center",
                inline: "nearest",
                behavior: "auto"
            });
            const rect = element.getBoundingClientRect();
            let visibleTop = 0;
            let visibleBottom = window.innerHeight;
            for (let ancestor = element.parentElement; ancestor;
                 ancestor = ancestor.parentElement) {
                const style = getComputedStyle(ancestor);
                if (!/auto|scroll|overlay|hidden|clip/.test(
                    style.overflowY + style.overflow
                )) continue;
                const ancestorRect = ancestor.getBoundingClientRect();
                visibleTop = Math.max(
                    visibleTop,
                    ancestorRect.top + ancestor.clientTop
                );
                visibleBottom = Math.min(
                    visibleBottom,
                    ancestorRect.top + ancestor.clientTop +
                        ancestor.clientHeight
                );
            }
            return {
                top: rect.top,
                bottom: rect.bottom,
                visibleTop,
                visibleBottom,
                scrollPositions
            };
        }"""
    )
    print(
        f"Scrolled {description} into the application panel "
        f"({len(visibility['scrollPositions'])} scroll containers; "
        f"scroll positions={visibility['scrollPositions']}; "
        f"element y={round(visibility['top'])}-"
        f"{round(visibility['bottom'])}, visible y="
        f"{round(visibility['visibleTop'])}-"
        f"{round(visibility['visibleBottom'])}).",
        flush=True,
    )
    if (
        visibility["bottom"] <= visibility["visibleTop"]
        or visibility["top"] >= visibility["visibleBottom"]
    ):
        raise RuntimeError(
            f"Could not reveal {description} inside LinkedIn's application panel. "
            "The panel was left open for manual review."
        )


def is_left_results_card(page, card):
    bounds = card.bounding_box()
    if not bounds or bounds["width"] <= 0:
        return False
    viewport_width = page.evaluate("() => window.innerWidth")
    card_center = bounds["x"] + bounds["width"] / 2
    return card_center < viewport_width * 0.5


def listing_card_and_link(page, job):
    card = page.locator(
        f'[role="button"][componentkey="job-card-component-ref-{job["id"]}"]'
    ).first
    if card.count() and card.is_visible() and is_left_results_card(page, card):
        return card, card

    links = page.locator(
        f'a[href*="/jobs/view/"][href*="{job["id"]}"]'
    )
    for index in range(links.count()):
        link = links.nth(index)
        if not link.is_visible():
            continue
        card = link.locator("xpath=ancestor::li[1]")
        if (
            card.count()
            and card.is_visible()
            and is_left_results_card(page, card)
        ):
            return card, link
    return None, None


def scroll_listing_card_into_results_view(card, job_id):
    visibility = card.evaluate(
        """element => {
            const results = element.closest(
                '[data-testid="lazy-column"]' +
                '[componentkey="SearchResultsMainContent"]'
            );
            if (!results) {
                throw new Error("LinkedIn search results column was not found.");
            }

            const scrollAncestors = [];
            for (let ancestor = element.parentElement; ancestor;
                 ancestor = ancestor.parentElement) {
                if (ancestor.scrollHeight > ancestor.clientHeight + 1) {
                    scrollAncestors.push(ancestor);
                }
                if (ancestor === results) break;
            }

            const scrollContainer = scrollAncestors[0];
            const before = scrollContainer ? scrollContainer.scrollTop : null;
            if (scrollContainer) {
                const elementRect = element.getBoundingClientRect();
                const containerRect = scrollContainer.getBoundingClientRect();
                const visibleTop =
                    containerRect.top + scrollContainer.clientTop;
                const targetTop =
                    elementRect.top - visibleTop -
                    (scrollContainer.clientHeight - elementRect.height) / 2;
                scrollContainer.scrollTop += targetTop;
            }
            element.scrollIntoView({
                block: "center",
                inline: "nearest",
                behavior: "auto"
            });

            const elementRect = element.getBoundingClientRect();
            const resultsRect = results.getBoundingClientRect();
            const resultsTop = resultsRect.top + results.clientTop;
            const resultsBottom = resultsTop + results.clientHeight;
            return {
                containerFound: true,
                scrollContainerFound: Boolean(scrollContainer),
                scrollBefore: before,
                scrollAfter: scrollContainer ? scrollContainer.scrollTop : null,
                elementTop: elementRect.top,
                elementBottom: elementRect.bottom,
                resultsTop,
                resultsBottom
            };
        }"""
    )
    print(
        f"Scrolled listing {job_id} in LinkedIn's left results pane "
        f"(scrollTop={visibility['scrollBefore']} -> "
        f"{visibility['scrollAfter']}; card y="
        f"{round(visibility['elementTop'])}-"
        f"{round(visibility['elementBottom'])}, results y="
        f"{round(visibility['resultsTop'])}-"
        f"{round(visibility['resultsBottom'])}).",
        flush=True,
    )
    if (
        visibility["elementBottom"] <= visibility["resultsTop"]
        or visibility["elementTop"] >= visibility["resultsBottom"]
    ):
        raise RuntimeError(
            f"Could not scroll listing {job_id} into LinkedIn's left results "
            "pane. Stopping for review."
        )


def canonical_job_url(href):
    parsed = urlparse(href)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"linkedin.com", "www.linkedin.com"}
        or not parsed.path.startswith("/jobs/view/")
    ):
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def find_jobs(page):
    cards = page.locator(
        '[role="button"][componentkey^="job-card-component-ref-"]'
    )
    jobs = {}

    for index in range(cards.count()):
        card = cards.nth(index)
        if not card.is_visible() or not is_left_results_card(page, card):
            continue
        component_key = card.get_attribute("componentkey") or ""
        match = re.fullmatch(r"job-card-component-ref-(\d+)", component_key)
        if not match:
            continue

        try:
            card_bounds = card.bounding_box()
            if not card_bounds:
                continue
            card_text = card.inner_text().strip()
        except PlaywrightError:
            continue
        dismiss_buttons = card.locator('button[aria-label^="Dismiss "]')
        dismiss_label = (
            dismiss_buttons.first.get_attribute("aria-label")
            if dismiss_buttons.count()
            else ""
        )
        title_match = re.fullmatch(r"Dismiss (.+) job", dismiss_label or "")
        if title_match:
            title = title_match.group(1)
        else:
            title_lines = [
                line.strip()
                for line in card_text.splitlines()
                if line.strip() and line.strip().casefold() != "selected"
            ]
            title = title_lines[0] if title_lines else "(untitled listing)"

        job = jobs.setdefault(
            match.group(1),
            {
                "id": match.group(1),
                "url": f"https://www.linkedin.com/jobs/view/{match.group(1)}",
                "title": title,
                "card_text": card_text,
                "card_y": card_bounds["y"],
            },
        )

    return [
        {key: value for key, value in job.items() if key != "card_y"}
        for job in sorted(jobs.values(), key=lambda candidate: candidate["card_y"])
    ]


def wait_for_search_results(page):
    try:
        wait_for_visible_locator(
            page,
            (
                '[role="button"][componentkey^="job-card-component-ref-"]',
                'a[href*="/jobs/view/"]',
            ),
            "job card",
            timeout_ms=45_000,
        )
    except RuntimeError as error:
        raise RuntimeError(
            "LinkedIn search results did not load. Check the Chrome window for "
            "a sign-in, verification, or network prompt, then rerun the command."
        ) from error


def current_search_results_page(page):
    indicators = page.locator(
        'button[data-testid^="pagination-indicator-"][aria-current="true"]'
    )
    visible_indicators = [
        indicators.nth(index)
        for index in range(indicators.count())
        if indicators.nth(index).is_visible()
    ]
    if len(visible_indicators) > 1:
        raise RuntimeError(
            "LinkedIn displayed multiple active search-results page indicators."
        )
    if not visible_indicators:
        return None
    return visible_indicators[0].get_attribute("aria-label")


def advance_search_results_page(page):
    current_page = current_search_results_page(page)
    if current_page is None:
        return False
    current_job_ids = {job["id"] for job in find_jobs(page)}

    next_buttons = page.locator(
        'button[data-testid="pagination-controls-next-button-visible"]'
    )
    visible_buttons = [
        next_buttons.nth(index)
        for index in range(next_buttons.count())
        if next_buttons.nth(index).is_visible()
    ]
    if not visible_buttons:
        return False
    if len(visible_buttons) > 1:
        raise RuntimeError(
            "LinkedIn displayed multiple visible next-page controls."
        )

    next_button = visible_buttons[0]
    if not next_button.is_enabled():
        return False
    print(
        f"Moving from search results {current_page} to the next page.",
        flush=True,
    )
    click_with_visible_cursor(page, next_button, "next search-results page")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError(
                "LinkedIn closed while moving to the next search-results page."
            )
        new_page = current_search_results_page(page)
        if new_page is not None and new_page != current_page:
            new_job_ids = {job["id"] for job in find_jobs(page)}
            if new_job_ids and new_job_ids != current_job_ids:
                print(f"Loaded LinkedIn search results {new_page}.", flush=True)
                return True
        time.sleep(0.25)
    raise RuntimeError(
        f"LinkedIn did not advance from {current_page} after clicking the "
        "next search-results page button."
    )


def is_signed_out(page):
    if "/authwall" in page.url or "/login" in page.url:
        return True
    for selector in ('a[href*="/login"]', 'button'):
        controls = page.locator(selector)
        try:
            for index in range(controls.count()):
                control = controls.nth(index)
                if (
                    control.inner_text().strip().casefold() == "sign in"
                    and control.is_visible()
                ):
                    return True
        except PlaywrightError:
            continue
    return False


def open_sign_in_form(page):
    if visible_locator(page, EMAIL_FIELD_SELECTORS) is not None:
        return
    if urlparse(page.url).path.startswith("/login"):
        try:
            wait_for_visible_locator(page, EMAIL_FIELD_SELECTORS, "email textbox")
        except RuntimeError as error:
            raise RuntimeError(
                "LinkedIn's login page loaded, but the sign-in form was not available."
            ) from error
        return

    links = page.locator('a[href*="/login"], a[href*="/uas/login"]')
    for index in range(links.count()):
        link = links.nth(index)
        href = link.get_attribute("href") or ""
        parsed = urlparse(href if href.startswith("http") else f"https://www.linkedin.com{href}")
        if (
            link.inner_text().strip().casefold() != "sign in"
            or not link.is_visible()
            or parsed.scheme != "https"
            or parsed.hostname not in {"linkedin.com", "www.linkedin.com"}
            or not parsed.path.startswith(("/login", "/uas/login"))
        ):
            continue
        try:
            link.click(timeout=5_000)
        except PlaywrightTimeoutError:
            link.click(force=True)
        break
    else:
        buttons = page.get_by_role("button", name=re.compile(r"^\s*Sign in\s*$", re.I))
        clicked = False
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if not button.is_visible():
                continue
            try:
                button.click(timeout=5_000)
            except PlaywrightTimeoutError:
                button.click(force=True)
            clicked = True
            break
        if not clicked:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

    try:
        wait_for_visible_locator(page, EMAIL_FIELD_SELECTORS, "email textbox")
    except RuntimeError as error:
        raise RuntimeError(
            "LinkedIn's Sign in control was selected, but its sign-in form did not "
            f"open. Current page: {page.url!r}."
        ) from error


def sign_in_with_env_credentials(page, email, password):
    print("Signing in to LinkedIn with credentials from .env...", flush=True)
    try:
        open_sign_in_form(page)
        page.bring_to_front()
        email_field = wait_for_visible_locator(
            page, EMAIL_FIELD_SELECTORS, "email textbox"
        )
        print("Found the visible LinkedIn email textbox.", flush=True)
        password_field = wait_for_visible_locator(
            page, PASSWORD_FIELD_SELECTORS, "password textbox"
        )
        print("Found the visible LinkedIn password textbox.", flush=True)
        submit_buttons = page.locator(
            'button[type="submit"][aria-label="Sign in"], '
            'button[type="submit"][data-tracking-control-name*="sign-in"], '
            'form button[type="submit"]'
        ).filter(has_text=re.compile(r"^\s*Sign in\s*$", re.IGNORECASE))
        submit_button = None
        for index in range(submit_buttons.count()):
            button = submit_buttons.nth(index)
            if button.is_visible():
                submit_button = button
                break
        if submit_button is None:
            raise RuntimeError(
                "Could not find LinkedIn's visible Sign in button. "
                "The credentials were not entered."
            )

        email_field.click()
        if not email_field.evaluate("(element) => document.activeElement === element"):
            raise RuntimeError(
                "Could not focus LinkedIn's email textbox. The credentials were not entered."
            )
        print("Email textbox focused; entering the configured email.", flush=True)
        email_field.fill(email)
        if email_field.input_value() != email:
            raise RuntimeError(
                "LinkedIn's email textbox did not retain the configured value. "
                "The password was not entered."
            )

        password_field.click()
        if not password_field.evaluate(
            "(element) => document.activeElement === element"
        ):
            raise RuntimeError(
                "Could not focus LinkedIn's password textbox. The Sign in button "
                "was not clicked."
            )
        print("Password textbox focused; entering the configured password.", flush=True)
        password_field.fill(password)
        if password_field.input_value() != password:
            raise RuntimeError(
                "LinkedIn's password textbox did not retain the configured value. "
                "The Sign in button was not clicked."
            )

        if (
            email_field.input_value() != email
            or password_field.input_value() != password
        ):
            raise RuntimeError(
                "LinkedIn's sign-in fields did not retain the configured values. "
                "The Sign in button was not clicked."
            )

        print("Both LinkedIn sign-in fields are populated; clicking Sign in.", flush=True)
        submit_button.click(timeout=10_000)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "LinkedIn's sign-in form or blue Sign in button was not available. "
            "No application was submitted."
        ) from error
    print(
        "LinkedIn sign-in submitted. Complete any verification in Chrome; "
        "the runner will continue after sign-in is detected.",
        flush=True,
    )


def wait_for_sign_in(page, timeout_seconds=300):
    if not is_signed_out(page):
        return
    email = os.getenv("LINKEDIN_EMAIL", "").strip()
    password = os.getenv("LINKEDIN_PASSWORD", "")
    if bool(email) != bool(password):
        raise RuntimeError(
            "Set both LINKEDIN_EMAIL and LINKEDIN_PASSWORD in the local .env file, "
            "or leave both empty to sign in manually."
        )
    open_sign_in_form(page)
    if email and password:
        sign_in_with_env_credentials(page, email, password)

    print(
        "Sign in to LinkedIn or complete any verification in the Chrome window. "
        "The runner will continue after sign-in is detected.",
        flush=True,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError("The LinkedIn Chrome page was closed before sign-in.")
        if not is_signed_out(page) and "linkedin.com" in (urlparse(page.url).hostname or ""):
            print("LinkedIn sign-in detected.", flush=True)
            return
        time.sleep(1)
    raise RuntimeError(
        "LinkedIn sign-in was not detected within five minutes. Sign in in Chrome "
        "and rerun the command."
    )


def has_application_success(page):
    try:
        return bool(SUCCESS_PATTERN.search(page.locator("body").inner_text()))
    except PlaywrightError:
        return False


def wait_for_application_success(page, timeout_seconds=300):
    print(
        "Waiting for LinkedIn to confirm the application. If a required step "
        "needs manual input, complete it in Chrome; the runner will continue "
        "automatically after success is detected.",
        flush=True,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if has_application_success(page):
            print(
                "LinkedIn confirmed the application submission.",
                flush=True,
            )
            return True
        if page.is_closed():
            raise RuntimeError(
                "LinkedIn closed before application success could be verified."
            )
        time.sleep(0.5)
    print(
        "LinkedIn did not confirm the application within five minutes.",
        flush=True,
    )
    return False


def dismiss_post_application_prompt(page):
    success_modal = page.locator("div.artdeco-modal__content").filter(
        has_text=SUCCESS_PATTERN
    )
    buttons = success_modal.locator(
        'button[data-view-name="seeker-next-best-action-card-cta-with-impression"]'
    )
    deadline = time.monotonic() + 5
    not_now_button = None
    while time.monotonic() < deadline:
        for index in range(buttons.count()):
            candidate = buttons.nth(index)
            if (
                candidate.is_visible()
                and candidate.inner_text().strip().casefold() == "not now"
            ):
                not_now_button = candidate
                break
        if not_now_button is not None:
            break
        if page.is_closed():
            raise RuntimeError(
                "LinkedIn closed before its post-application prompt could be handled."
            )
        time.sleep(0.1)

    if not_now_button is None:
        return

    click_with_visible_cursor(
        page,
        not_now_button,
        "post-application Not now button",
    )
    print("Dismissed LinkedIn's post-application prompt.", flush=True)


def review_application_heading(page):
    headings = page.locator("h3").filter(
        has_text=re.compile(r"^\s*Review your application\s*$", re.IGNORECASE)
    )
    for index in range(headings.count()):
        heading = headings.nth(index)
        if heading.is_visible():
            return heading
    return None


def application_dialog_submit_button(page):
    submit_pattern = re.compile(r"^\s*Submit application\s*$", re.IGNORECASE)
    dialog_selectors = (
        'dialog[open]',
        '[role="dialog"]',
        "div.artdeco-modal__content",
    )
    for selector in dialog_selectors:
        dialogs = page.locator(selector).filter(
            has_text=re.compile(r"\bApply to\b", re.IGNORECASE)
        )
        visible_dialogs = [
            dialogs.nth(index)
            for index in range(dialogs.count())
            if dialogs.nth(index).is_visible()
        ]
        if not visible_dialogs:
            continue

        buttons = visible_dialogs[0].get_by_role(
            "button",
            name=submit_pattern,
        )
        visible_buttons = [
            buttons.nth(index)
            for index in range(buttons.count())
            if buttons.nth(index).is_visible()
        ]
        if len(visible_buttons) > 1:
            raise RuntimeError(
                "The LinkedIn application dialog has multiple visible Submit "
                "application buttons. Stopping for review."
            )
        if visible_buttons:
            return visible_buttons[0]
    return None


def application_step(page):
    submit_button = application_dialog_submit_button(page)
    if submit_button is not None:
        progress_labels = page.get_by_text(
            re.compile(r"\b\d+\s*/\s*\d+\s+pages?\b", re.IGNORECASE)
        )
        progress = next(
            (
                progress_labels.nth(index).inner_text().strip()
                for index in range(progress_labels.count())
                if progress_labels.nth(index).is_visible()
            ),
            "Application submission",
        )
        return progress, submit_button

    if review_application_heading(page) is not None:
        action_pattern = re.compile(r"^\s*Submit application\s*$", re.IGNORECASE)
        action_buttons = page.locator("button[componentkey]").filter(
            has_text=action_pattern
        )
        visible_buttons = [
            action_buttons.nth(index)
            for index in range(action_buttons.count())
            if action_buttons.nth(index).is_visible()
        ]
        if not visible_buttons:
            action_buttons = page.get_by_role("button", name=action_pattern)
            visible_buttons = [
                action_buttons.nth(index)
                for index in range(action_buttons.count())
                if action_buttons.nth(index).is_visible()
            ]
        if len(visible_buttons) > 1:
            raise RuntimeError(
                "The Review your application page has multiple visible Submit "
                "application buttons. Stopping for review."
            )
        progress_labels = page.get_by_text(
            re.compile(r"\b\d+\s*/\s*\d+\s+pages?\b", re.IGNORECASE)
        )
        progress = next(
            (
                progress_labels.nth(index).inner_text().strip()
                for index in range(progress_labels.count())
                if progress_labels.nth(index).is_visible()
            ),
            "Review your application",
        )
        return progress, visible_buttons[0] if visible_buttons else None

    additional_headings = page.locator("p")
    visible_additional_headings = []
    for index in range(additional_headings.count()):
        heading = additional_headings.nth(index)
        if (
            heading.is_visible()
            and heading.inner_text().strip().casefold() == "additional questions"
        ):
            visible_additional_headings.append(heading)
    if visible_additional_headings:
        if len(visible_additional_headings) > 1:
            raise RuntimeError(
                "LinkedIn displayed multiple Additional Questions headings. "
                "Stopping for review."
            )
        dialog = page.locator('dialog[data-testid="dialog"]:visible')
        if dialog.count() != 1:
            raise RuntimeError(
                "Expected one visible LinkedIn application dialog for Additional "
                f"Questions, but found {dialog.count()}. Stopping for review."
            )
        next_buttons = dialog.locator(
            'button[data-testid="pagination-controls-next-button-visible"], '
            'button[componentkey]'
        ).filter(has_text=re.compile(r"^\s*Next\s*$", re.IGNORECASE))
        question_controls = page.locator(
            'input[aria-label^="How many years of professional software '
            'engineering experience" i], '
            'input[aria-label^="How many years of hands-on Microsoft Azure '
            'experience" i], '
            'input[aria-label^="Do you have professional experience '
            'developing applications with C#/.NET" i], '
            'input[aria-label^="Do you have client-facing technical consulting '
            'or pre-sales experience" i], '
            'input[aria-label^="Have you personally designed and deployed '
            'production software systems" i]'
        )
        visible_question_controls = [
            question_controls.nth(index)
            for index in range(question_controls.count())
            if question_controls.nth(index).is_visible()
        ]
        if not visible_question_controls:
            raise UnsupportedApplicationQuestions(
                "The Additional Questions heading is visible, but none of the "
                "configured question fields were found."
            )
        visible_next_buttons = []
        for index in range(next_buttons.count()):
            button = next_buttons.nth(index)
            if button.is_visible():
                visible_next_buttons.append(button)
        if len(visible_next_buttons) != 1:
            raise RuntimeError(
                "Expected one visible Next button inside LinkedIn's Additional "
                f"Questions dialog, but found {len(visible_next_buttons)}. "
                "Stopping for review."
            )
        return (
            "Additional Questions",
            visible_next_buttons[0],
        )

    progress_labels = page.get_by_text(
        re.compile(r"\b\d+\s*/\s*\d+\s+pages?\b", re.IGNORECASE)
    )
    for label_index in range(progress_labels.count()):
        progress_label = progress_labels.nth(label_index)
        if not progress_label.is_visible():
            continue

        for depth in range(1, 21):
            step = progress_label.locator("xpath=" + ".." + "/.." * (depth - 1))
            step_text = re.sub(r"\s+", " ", step.inner_text())
            progress_match = re.search(
                r"\b\d+\s*/\s*\d+\s+pages?\b", step_text, re.IGNORECASE
            )
            if not progress_match or not re.search(
                r"\bApply to\b", step_text, re.IGNORECASE
            ):
                continue

            location_fields = step.locator(
                'input[aria-label="What is your current location?"]'
            )
            visible_location_fields = [
                location_fields.nth(field_index)
                for field_index in range(location_fields.count())
                if location_fields.nth(field_index).is_visible()
            ]
            if len(visible_location_fields) > 1:
                raise RuntimeError(
                    "The LinkedIn application step has multiple visible current "
                    "location fields. Stopping for review."
                )
            if visible_location_fields:
                location_field = visible_location_fields[0]
                if not location_field.input_value().strip():
                    location_field.fill("Navarre, FL")
                    print(
                        "Filled the blank current-location field with Navarre, FL.",
                        flush=True,
                    )

            action_pattern = re.compile(
                r"^\s*(?:Next|Review|Submit application)\s*$",
                re.IGNORECASE,
            )
            action_buttons = step.locator("button[componentkey]").filter(
                has_text=action_pattern
            )
            if action_buttons.count() == 0:
                action_buttons = page.locator("button[componentkey]").filter(
                    has_text=action_pattern
                )
            if action_buttons.count() == 0:
                action_buttons = page.get_by_role("button", name=action_pattern)
            visible_buttons = [
                action_buttons.nth(button_index)
                for button_index in range(action_buttons.count())
                if action_buttons.nth(button_index).is_visible()
            ]
            if len(visible_buttons) > 1:
                raise RuntimeError(
                    "The LinkedIn application step has multiple visible action "
                    "buttons. Stopping for review."
                )
            return progress_match.group(0), (
                visible_buttons[0] if visible_buttons else None
            )
    return None


def visible_question_controls(page, selector, description):
    controls = page.locator(selector)
    visible_controls = [
        controls.nth(index)
        for index in range(controls.count())
        if controls.nth(index).is_visible()
    ]
    if not visible_controls:
        return []
    if len(visible_controls) > 2:
        raise RuntimeError(
            f"LinkedIn displayed multiple matching controls for {description}. "
            "Stopping for review."
        )
    return visible_controls


def answer_additional_questions(page):
    headings = page.locator("p")
    visible_headings = []
    for index in range(headings.count()):
        heading = headings.nth(index)
        if (
            heading.is_visible()
            and heading.inner_text().strip().casefold() == "additional questions"
        ):
            visible_headings.append(heading)
    if not visible_headings:
        return
    if len(visible_headings) > 1:
        raise RuntimeError(
            "LinkedIn displayed multiple Additional Questions headings. "
            "Stopping for review."
        )

    text_questions = (
        (
            "professional software engineering experience",
            'input[type="text"][aria-label^="How many years of professional software engineering experience" i]',
            "15",
        ),
        (
            "Microsoft Azure experience",
            'input[type="text"][aria-label^="How many years of hands-on Microsoft Azure experience" i]',
            "12",
        ),
    )
    answered_questions = 0
    for description, selector, value in text_questions:
        textboxes = visible_question_controls(page, selector, description)
        if not textboxes:
            continue
        if len(textboxes) != 1:
            raise RuntimeError(
                f"LinkedIn displayed multiple text fields for {description}. "
                "Stopping for review."
            )
        textbox = textboxes[0]
        textbox.fill(value)
        if textbox.input_value() != value:
            raise RuntimeError(
                f"LinkedIn did not retain the answer for {description}. "
                "Stopping for review."
            )
        print(
            f"Entered {value} for the {description} question.",
            flush=True,
        )
        answered_questions += 1

    radio_questions = (
        (
            "C#/.NET professional experience",
            'input[type="radio"][aria-label^="Do you have professional experience developing applications with C#/.NET" i]',
        ),
        (
            "client-facing technical consulting or pre-sales experience",
            'input[type="radio"][aria-label^="Do you have client-facing technical consulting or pre-sales experience" i]',
        ),
        (
            "production software systems experience",
            'input[type="radio"][aria-label^="Have you personally designed and deployed production software systems" i]',
        ),
    )
    for description, selector in radio_questions:
        radios = page.locator(selector)
        if radios.count() == 0:
            continue
        if radios.count() != 2:
            raise RuntimeError(
                f"Expected Yes and No radio inputs for {description}, but found "
                f"{radios.count()}. Stopping for review."
            )
        yes_radio = radios.nth(0)
        if not yes_radio.is_checked():
            yes_radio.evaluate("(element) => element.click()")
        deadline = time.monotonic() + 2
        while not yes_radio.is_checked() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not yes_radio.is_checked():
            raise RuntimeError(
                f"Could not select Yes for the {description} question. "
                "Stopping for review."
            )
        print(
            f"Selected Yes for the {description} question.",
            flush=True,
        )
        answered_questions += 1

    if not answered_questions:
        raise UnsupportedApplicationQuestions(
            "The Additional Questions page was detected, but none of the "
            "configured question fields were found."
        )


def discard_incomplete_application_dialog(page):
    application_dialog = None
    for selector in (
        'dialog[data-testid="dialog"]:visible',
        'div.artdeco-modal__content:visible',
        '[role="dialog"]:visible',
    ):
        candidates = page.locator(selector).filter(
            has_text=re.compile(r"\bApply to\b", re.IGNORECASE)
        )
        if candidates.count() > 1:
            raise RuntimeError(
                "Could not uniquely locate the incomplete application dialog "
                "to close it safely."
            )
        if candidates.count() == 1:
            application_dialog = candidates.first
            break
    if application_dialog is None:
        raise RuntimeError(
            "Could not uniquely locate the incomplete application dialog "
            "to close it safely."
        )

    close_buttons = application_dialog.locator(
        'button[aria-label="Close"], '
        'button[aria-label="Dismiss"], '
        'button[data-testid*="close" i]'
    )
    visible_buttons = [
        close_buttons.nth(index)
        for index in range(close_buttons.count())
        if close_buttons.nth(index).is_visible()
    ]
    if len(visible_buttons) > 1:
        raise RuntimeError(
            "The incomplete application dialog has multiple possible close "
            "buttons; refusing to guess."
        )
    if visible_buttons:
        click_with_visible_cursor(
            page,
            visible_buttons[0],
            "close incomplete application dialog",
        )
    else:
        page.keyboard.press("Escape")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError(
                "LinkedIn closed while leaving the incomplete application."
            )
        save_heading = page.get_by_text(
            re.compile(r"^\s*Save this application\?\s*$", re.IGNORECASE)
        )
        visible_save_headings = [
            save_heading.nth(index)
            for index in range(save_heading.count())
            if save_heading.nth(index).is_visible()
        ]
        if visible_save_headings:
            discard_buttons = page.get_by_role(
                "button",
                name=re.compile(r"^\s*Discard\s*$", re.IGNORECASE),
            )
            visible_discard_buttons = [
                discard_buttons.nth(index)
                for index in range(discard_buttons.count())
                if discard_buttons.nth(index).is_visible()
            ]
            if len(visible_discard_buttons) != 1:
                raise RuntimeError(
                    "LinkedIn asked whether to save the application, but there "
                    "was not exactly one visible Discard button."
                )
            click_with_visible_cursor(
                page,
                visible_discard_buttons[0],
                "Discard incomplete application",
            )
            print(
                "Discarded the incomplete application without submitting.",
                flush=True,
            )

            discard_deadline = time.monotonic() + 5
            while time.monotonic() < discard_deadline:
                if page.is_closed():
                    raise RuntimeError(
                        "LinkedIn closed while discarding the incomplete "
                        "application."
                    )
                if not application_dialog.is_visible():
                    return
                time.sleep(0.1)
            raise RuntimeError(
                "Clicked Discard, but LinkedIn did not close the incomplete "
                "application dialog."
            )

        if not application_dialog.is_visible():
            print(
                "Closed the incomplete application dialog without submitting.",
                flush=True,
            )
            return
        time.sleep(0.1)
    raise RuntimeError(
        "Could not close the incomplete application dialog safely. "
        "Stopping before moving to another job."
    )


def disable_follow_company_on_review(page):
    if review_application_heading(page) is None:
        return

    follow_checkboxes = page.locator(
        'input[type="checkbox"][aria-label^="Follow "]'
    )
    visible_checkboxes = [
        follow_checkboxes.nth(index)
        for index in range(follow_checkboxes.count())
        if follow_checkboxes.nth(index).is_visible()
    ]
    if not visible_checkboxes:
        print(
            "No visible follow-company checkbox on the review page; continuing.",
            flush=True,
        )
        return
    if len(visible_checkboxes) > 1:
        raise RuntimeError(
            "The Review your application page has multiple visible "
            "follow-company checkboxes. Stopping for review."
        )

    checkbox = visible_checkboxes[0]
    center_locator_in_scroll_container(
        checkbox,
        "follow-company checkbox",
    )
    print(
        "Follow-company checkbox is "
        f"{'checked' if checkbox.is_checked() else 'already unchecked'}.",
        flush=True,
    )
    if checkbox.is_checked():
        checkbox.evaluate(
            """element => {
                if (element.checked) element.click();
            }"""
        )
        deadline = time.monotonic() + 2
        while checkbox.is_checked() and time.monotonic() < deadline:
            time.sleep(0.1)
        if checkbox.is_checked():
            raise RuntimeError(
                "Could not uncheck the follow-company checkbox. "
                "Stopping before submission."
            )
        print(
            "Unchecked the follow-company checkbox on the review page.",
            flush=True,
        )


def advance_application_steps(page):
    max_steps = 20
    clicked_steps = 0

    while clicked_steps < max_steps:
        deadline = time.monotonic() + 15
        step = None
        while time.monotonic() < deadline:
            step = application_step(page)
            if step is not None:
                break
            if page.is_closed():
                raise RuntimeError(
                    "LinkedIn closed before the application step appeared."
                )
            time.sleep(0.25)

        if step is None:
            if clicked_steps:
                raise UnrecognizedApplicationStep(
                    "The LinkedIn application step did not appear after clicking "
                    "Next."
                )
            print(
                "No paginated LinkedIn application dialog was detected. "
                "Continue manually in Chrome.",
                flush=True,
            )
            return False

        progress, action_button = step
        answer_additional_questions(page)
        if progress == "Additional Questions":
            refreshed_step = application_step(page)
            if refreshed_step is None or refreshed_step[0] != progress:
                raise RuntimeError(
                    "The Additional Questions page changed while its answers "
                    "were being entered. Stopping before clicking Next."
                )
            action_button = refreshed_step[1]
        disable_follow_company_on_review(page)
        if action_button is None:
            if review_application_heading(page) is not None:
                raise RuntimeError(
                    "The Review your application page is visible, but its "
                    "Submit application button could not be found. Stopping "
                    "before submission."
                )
            print(
                f"LinkedIn application page {progress} has no Next, Review, or "
                "Submit application button. "
                "Leaving it open for manual review.",
                flush=True,
            )
            return clicked_steps > 0

        action = action_button.inner_text().strip()
        if action.casefold() == "next" and not action_button.is_enabled():
            raise RuntimeError(
                f"The Next button on application page {progress} is disabled "
                "after answering the questions. Stopping for review."
            )
        print(
            f"Found visible application action button: {action!r}; "
            f"enabled={action_button.is_enabled()}; "
            f"bounds={action_button.bounding_box()}.",
            flush=True,
        )
        if action.casefold() == "submit application":
            center_locator_in_scroll_container(
                action_button,
                "Submit application button",
            )
        print(
            f"Clicking {action} on LinkedIn "
            f"application page {progress}.",
            flush=True,
        )
        click_with_visible_cursor(
            page,
            action_button,
            f"{action} on application page {progress}",
        )
        if action.casefold() == "submit application":
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if has_application_success(page):
                    print(
                        "LinkedIn confirmed the application submission.",
                        flush=True,
                    )
                    return True
                if page.is_closed():
                    raise RuntimeError(
                        "LinkedIn closed after Submit application before success "
                        "could be verified."
                    )
                time.sleep(0.5)
            raise RuntimeError(
                "Clicked Submit application, but LinkedIn did not show a "
                "recognizable success message within 30 seconds. Verify the "
                "application status before retrying."
            )
        clicked_steps += 1

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if page.is_closed():
                raise RuntimeError(
                    "LinkedIn closed while advancing the application."
                )
            next_step = application_step(page)
            if (
                next_step is not None
                and (
                    next_step[0] != progress
                    or review_application_heading(page) is not None
                )
            ):
                break
            time.sleep(0.25)
        else:
            raise UnrecognizedApplicationStep(
                f"LinkedIn did not advance from application page {progress} "
                "after clicking Next."
            )

    raise RuntimeError(
        f"Stopped after {max_steps} application pages to avoid an unexpected loop. "
        "Continue reviewing the application manually."
    )


def apply_to_job(page, job):
    print(
        f"\nTarget job: {job['title']} ({job['id']})\n"
        f"Listing URL: {job['url']}\n"
        "Finding this job in the left results pane...",
        flush=True,
    )
    card, listing_link = listing_card_and_link(page, job)
    if card is None or listing_link is None:
        print(
            "This job is no longer visible in the left results pane; skipping it.",
            flush=True,
        )
        return False

    try:
        scroll_listing_card_into_results_view(card, job["id"])
        click_with_visible_cursor(page, listing_link, f"listing {job['id']}")
    except PlaywrightError as error:
        raise RuntimeError(
            f"Could not select LinkedIn listing {job['id']} in the results pane."
        ) from error

    apply_buttons = page.locator(
        'button[aria-label^="Easy Apply"]'
    )
    if apply_buttons.count() == 0:
        apply_buttons = page.get_by_role(
            "button",
            name=re.compile(r"^\s*Easy Apply(?:\s+to this job)?\s*$", re.IGNORECASE),
        )
    apply_button = None
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        for index in range(apply_buttons.count()):
            candidate = apply_buttons.nth(index)
            bounds = candidate.bounding_box() if candidate.is_visible() else None
            viewport_width = page.evaluate("() => window.innerWidth")
            if bounds and bounds["x"] + bounds["width"] / 2 >= viewport_width * 0.4:
                apply_button = candidate
                break
        if apply_button is not None:
            break
        if page.is_closed():
            raise RuntimeError("The LinkedIn results page was closed.")
        time.sleep(0.25)
    if apply_button is None:
        print(
            f"No Easy Apply button in the details for {job['id']}; leaving this "
            "card unchanged and checking the next listing.",
            flush=True,
        )
        return False

    print(
        "Easy Apply is available in the details pane. Opening the application flow. "
        "The runner will click available Next and Review buttons, uncheck the "
        "follow-company option on the review page if present, and click Submit "
        "application. "
        "Complete any required answers before the review page.",
        flush=True,
    )
    try:
        click_with_visible_cursor(page, apply_button, f"Easy Apply for {job['id']}")
    except PlaywrightError as error:
        raise RuntimeError(
            f"Could not click Easy Apply for listing {job['id']}."
        ) from error
    application_flow_started_at = time.monotonic()
    try:
        advance_application_steps(page)
    except UnsupportedApplicationQuestions as error:
        print(
            f"{error} Skipping job {job['id']} without submitting and moving "
            "to the next listing.",
            flush=True,
        )
        discard_incomplete_application_dialog(page)
        return False
    except UnrecognizedApplicationStep as error:
        print(
            f"{error} Waiting up to five minutes for success. If it remains "
            "unrecognized, the application will be discarded and skipped.",
            flush=True,
        )
    if not has_application_success(page):
        remaining_timeout = max(
            0,
            300 - (time.monotonic() - application_flow_started_at),
        )
        if not wait_for_application_success(
            page,
            timeout_seconds=remaining_timeout,
        ):
            discard_incomplete_application_dialog(page)
            print(
                f"Skipping job {job['id']} without recording or submitting it.",
                flush=True,
            )
            return False
    dismiss_post_application_prompt(page)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Preview LinkedIn Easy Apply listings or assist with applications."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Open eligible listings, advance available application Next buttons, "
            "and pause for your review and submission."
        ),
    )
    args = parser.parse_args()

    state = read_state()
    completed_ids = set(state)

    with sync_playwright() as playwright:
        if CHROME_PROFILE_DIR is None:
            raise RuntimeError(
                "Could not determine LOCALAPPDATA for the dedicated Chrome profile."
            )
        CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        print(
            f"Opening the dedicated LinkedIn Chrome profile at {CHROME_PROFILE_DIR}...",
            flush=True,
        )
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE_DIR),
                channel="chrome",
                headless=False,
                args=["--start-maximized"],
                no_viewport=True,
                timeout=30_000,
            )
        except (OSError, PlaywrightError) as error:
            raise RuntimeError(
                "Could not start Chrome with the dedicated LinkedIn profile. "
                "Close any Chrome window opened by another LinkedIn runner and retry."
            ) from error

        try:
            add_visible_cursor(context)
            page = context.pages[0] if context.pages else context.new_page()
            print(f"Navigating to {SEARCH_URL}", flush=True)
            try:
                page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightError as error:
                raise RuntimeError(
                    f"Chrome opened, but LinkedIn search navigation failed "
                    f"({page.url!r}). Check your connection and Chrome window."
                ) from error
            if args.apply:
                wait_for_sign_in(page)
                if "/jobs/search" not in page.url:
                    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
            elif is_signed_out(page):
                raise RuntimeError(
                    "LinkedIn redirected this search to its sign-in wall. "
                    "Run with --apply to sign in and continue."
                )

            wait_for_search_results(page)
            print(f"LinkedIn search results loaded: {page.url}", flush=True)
            if not args.apply:
                jobs = find_jobs(page)
                print(
                    f"Found {len(jobs)} LinkedIn listing(s) on the current "
                    "results page."
                )
                for job in jobs:
                    status = (
                        "already processed"
                        if job["id"] in completed_ids
                        else "ready"
                    )
                    print(f"- {job['title']} [{job['id']}]: {status}", flush=True)
                if not jobs:
                    print(
                        "No job cards were recognized in the left results pane. "
                        "No applications were started.",
                        flush=True,
                    )
                else:
                    print("\nPreview only. Sign in and run with --apply to start.")
                return

            wait_for_sign_in(page)
            applied_this_run = 0
            while True:
                jobs = find_jobs(page)
                if not jobs:
                    if applied_this_run == 0:
                        print(
                            "No job cards were recognized in the left results "
                            "pane. No applications were started.",
                            flush=True,
                        )
                    break

                pending = [
                    job for job in jobs if job["id"] not in completed_ids
                ]
                pending_ids = {job["id"] for job in pending}
                current_page = current_search_results_page(page)
                print(
                    f"Found {len(jobs)} LinkedIn listing(s) on "
                    f"{current_page or 'the current results page'}."
                )
                for job in jobs:
                    status = (
                        "already processed"
                        if job["id"] not in pending_ids
                        else "ready"
                    )
                    print(f"- {job['title']} [{job['id']}]: {status}", flush=True)

                for job in pending:
                    if apply_to_job(page, job):
                        state[job["id"]] = {
                            "url": job["url"],
                            "submitted_at": datetime.now(timezone.utc).isoformat(),
                        }
                        write_state(state)
                        completed_ids.add(job["id"])
                        applied_this_run += 1
                        print(
                            "LinkedIn showed the success message; application "
                            "recorded."
                        )

                if not advance_search_results_page(page):
                    break
            print(f"\nFinished. Recorded {applied_this_run} application(s) in this run.")
        finally:
            if args.apply:
                try:
                    input(
                        "\nLinkedIn Chrome is being kept open for inspection. "
                        "Press Enter here when you are ready to close it..."
                    )
                except EOFError:
                    print(
                        "No interactive terminal is available; closing LinkedIn Chrome.",
                        flush=True,
                    )
            context.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        raise SystemExit(1) from error
