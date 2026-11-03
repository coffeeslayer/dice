import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


SEARCH_URL = (
    "https://www.dice.com/jobs?filters.easyApply=true&filters.postedDate=THREE"
    "&filters.workplaceTypes=Remote&q=.net+developer+with+react"
)
CHROME_PROFILE_DIR = (
    Path(os.environ["LOCALAPPDATA"]) / "DiceJobSearch" / "ChromeProfile"
    if os.environ.get("LOCALAPPDATA")
    else None
)
STATE_FILE = Path(__file__).parent / ".dice-application-state.json"
ENV_FILE = Path(__file__).parent / ".env"
JOB_ID_PATTERN = re.compile(r"/job-detail/([^/?#]+)")
RESULT_COUNT_PATTERN = re.compile(r"(\d+)\s+results?\b", re.IGNORECASE)
load_dotenv(ENV_FILE)


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


def find_jobs(page):
    body_text = page.locator("body").inner_text()
    result_count_match = RESULT_COUNT_PATTERN.search(body_text)
    if not result_count_match:
        raise RuntimeError(
            "Could not determine the result count. Confirm the Dice search page "
            "loaded, then try again."
        )

    result_count = int(result_count_match.group(1))
    links = page.locator('a[href*="/job-detail/"]')
    jobs = {}

    for index in range(links.count()):
        link = links.nth(index)
        href = link.get_attribute("href")
        if not href:
            continue
        match = JOB_ID_PATTERN.search(href)
        if not match:
            continue

        job_id = match.group(1)
        job_url = urljoin(SEARCH_URL, href)
        parsed_url = urlparse(job_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "www.dice.com":
            continue
        parent_text = link.locator("xpath=..").inner_text().strip()
        job = jobs.setdefault(
            job_id,
            {"id": job_id, "url": job_url, "card_text": ""},
        )
        if len(parent_text) > len(job["card_text"]):
            job["card_text"] = parent_text

    results = list(jobs.values())[:result_count]
    if len(results) != result_count:
        raise RuntimeError(
            f"Dice reports {result_count} results, but only {len(results)} "
            "listing links were found. No applications were submitted."
        )
    return results


def wait_for_search_results(page):
    try:
        page.get_by_text(RESULT_COUNT_PATTERN).first.wait_for(
            state="visible",
            timeout=45_000,
        )
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            f"Chrome opened, but Dice search results did not load. "
            f"Current page: {page.url!r}. Check the browser for a sign-in, "
            "network, or site prompt, then rerun the command."
        ) from error


def accept_cookies_if_present(page):
    accept_button = page.get_by_role(
        "button",
        name=re.compile(r"^\s*Accept all\s*$", re.IGNORECASE),
        exact=True,
    )
    try:
        accept_button.wait_for(state="visible", timeout=2_000)
        accept_button.click(timeout=5_000)
    except PlaywrightTimeoutError:
        print(
            "Cookie consent button not available; continuing.",
            flush=True,
        )
    except PlaywrightError as error:
        print(
            f"Could not accept cookies ({error}); continuing.",
            flush=True,
        )
    else:
        print("Accepted all cookies.", flush=True)


def is_signed_out(page):
    login_link = page.get_by_role(
        "link",
        name=re.compile(r"^\s*Login\s*$", re.IGNORECASE),
        exact=True,
    )
    try:
        return login_link.is_visible()
    except PlaywrightError:
        return False


def sign_in_with_env_credentials(page, email, password):
    print("Signing in to Dice with credentials from .env...", flush=True)
    if not page.url.startswith("https://www.dice.com/dashboard/login"):
        page.goto("https://www.dice.com/dashboard/login", wait_until="domcontentloaded")

    email_field = page.locator('input[type="email"]').first
    try:
        email_field.wait_for(state="visible", timeout=20_000)
        email_field.fill(email)
        page.get_by_role("button", name="Continue with email", exact=True).click()
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Dice's email sign-in form was not available. No credentials were submitted."
        ) from error

    password_field = page.locator(
        'input[type="password"][autocomplete="current-password"], '
        'input[type="password"][name="password"]'
    ).first
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if page.locator(
            'input[name="firstName"], input[name="confirmPassword"]'
        ).count():
            raise RuntimeError(
                "Dice showed account registration instead of an existing-account "
                "sign-in form. No registration was submitted; check the email in .env."
            )
        if password_field.is_visible():
            break
        time.sleep(0.5)
    else:
        print(
            "Dice did not show a password form. Complete any sign-in or "
            "verification step manually in Chrome.",
            flush=True,
        )
        return

    password_field.fill(password)
    form = password_field.locator("xpath=ancestor::form")
    submit_button = form.get_by_role(
        "button",
        name=re.compile(r"^(sign in|log in|continue)$", re.IGNORECASE),
    )
    try:
        submit_button.click(timeout=10_000)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Dice's password sign-in button was not available. "
            "The password was entered but not submitted."
        ) from error
    print(
        "Dice sign-in submitted. Waiting for the account session or any "
        "additional verification in Chrome...",
        flush=True,
    )


def wait_for_sign_in(page, timeout_seconds=300):
    if not is_signed_out(page):
        return

    email = os.getenv("DICE_EMAIL", "").strip()
    password = os.getenv("DICE_PASSWORD", "")
    if bool(email) != bool(password):
        raise RuntimeError(
            "Set both DICE_EMAIL and DICE_PASSWORD in the local .env file, "
            "or leave both empty to sign in manually."
        )
    if email and password:
        sign_in_with_env_credentials(page, email, password)
        if not is_signed_out(page):
            print("Dice sign-in successful.", flush=True)
            return

    print(
        "Complete Dice sign-in or any verification in the Chrome window. "
        "The runner will continue after sign-in is detected.",
        flush=True,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError("The Dice Chrome page was closed before sign-in.")
        if not is_signed_out(page) and "dice.com" in (urlparse(page.url).hostname or ""):
            print("Dice sign-in detected; continuing.", flush=True)
            return
        time.sleep(1)

    raise RuntimeError(
        "Dice sign-in was not detected within five minutes. Sign in in Chrome "
        "and rerun the command."
    )


def click_exact_button(page, name, label):
    button = page.get_by_role("button", name=name, exact=True)
    try:
        button.wait_for(state="visible", timeout=20_000)
        button.click()
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            f"Could not find the {label} button. The page may have changed or "
            "the application needs attention. Stopping without retrying."
        ) from error


def click_apply_action(page):
    apply_action = page.locator(
        'a[data-testid="apply-button"], '
        'button[data-testid="apply-button"], '
        '[role="button"][data-testid="apply-button"]'
    ).first
    try:
        apply_action.wait_for(state="visible", timeout=20_000)
        apply_action.click()
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Could not find Dice's apply link on the job page. "
            f"Current page: {page.url!r}. The listing may not support Easy Apply."
        ) from error


def find_question_group(page, question):
    question_pattern = re.compile(rf"^\s*{re.escape(question)}\s*$", re.IGNORECASE)
    group_pattern = re.compile(re.escape(question), re.IGNORECASE)

    named_groups = page.get_by_role("radiogroup", name=group_pattern)
    if named_groups.count() == 1 and named_groups.locator(
        'input[type="radio"], [role="radio"]'
    ).count() == 2:
        return named_groups

    fieldsets = page.locator("fieldset").filter(has_text=group_pattern)
    if fieldsets.count() == 1 and fieldsets.locator(
        'input[type="radio"], [role="radio"]'
    ).count() == 2:
        return fieldsets

    question_text = page.get_by_text(question_pattern).first
    if question_text.count() == 0:
        return None

    for depth in range(1, 8):
        ancestor = question_text.locator("xpath=" + "/.." * depth)
        if ancestor.locator('input[type="radio"], [role="radio"]').count() == 2:
            return ancestor
    return None


def find_radio_option(group, answer):
    accessible_option = group.get_by_role("radio", name=answer, exact=True)
    if accessible_option.count() == 1:
        return accessible_option

    radio_inputs = group.locator('input[type="radio"]')
    matches = []
    for index in range(radio_inputs.count()):
        radio = radio_inputs.nth(index)
        labels = radio.evaluate(
            """element => {
                const associated = element.labels
                    ? Array.from(element.labels).map(label => label.innerText)
                    : [];
                const parentLabel = element.closest('label');
                if (parentLabel) associated.push(parentLabel.innerText);
                return [
                    ...associated,
                    element.getAttribute('aria-label'),
                    element.value
                ].filter(Boolean).map(value => value.trim());
            }"""
        )
        if any(label.casefold() == answer.casefold() for label in labels):
            matches.append(radio)
    if len(matches) == 1:
        return matches[0]
    return None


def select_radio_option(option, question, answer):
    try:
        option.evaluate("(element) => element.click()")
        selected = option.evaluate(
            """element => element.matches('input[type="radio"]')
                ? element.checked
                : element.getAttribute('aria-checked') === 'true'"""
        )
    except PlaywrightError as error:
        raise RuntimeError(
            f"Could not select {answer!r} for {question!r}: {error}"
        ) from error
    if not selected:
        raise RuntimeError(
            f"Dice did not select {answer!r} for {question!r}. "
            "Stopping without advancing."
        )


def answer_known_application_questions(page):
    answers = (
        ("Can you work only on C2C or 1099?", "No"),
        ("Are you represented by a recruiter?", "No"),
        ("Are you legally Authorized to work in USA?", "Yes"),
    )
    groups = []
    for question, answer in answers:
        group = find_question_group(page, question)
        if group is None:
            raise RuntimeError(
                "Dice showed an application step without Submit, but the "
                f"expected question {question!r} was not found. Stopping "
                "without selecting answers."
            )
        groups.append((group, answer, question))

    for group, answer, question in groups:
        option = find_radio_option(group, answer)
        if option is None:
            radio_details = group.locator(
                'input[type="radio"], [role="radio"]'
            ).evaluate_all(
                """elements => elements.map(element => ({
                    tag: element.tagName,
                    role: element.getAttribute('role'),
                    name: element.getAttribute('aria-label'),
                    value: element.value,
                    labels: element.labels
                        ? Array.from(element.labels).map(label => label.innerText)
                        : [],
                    checked: element.checked
                }))"""
            )
            raise RuntimeError(
                f"Could not identify the {answer!r} radio for {question!r}. "
                f"Found controls: {radio_details!r}. Stopping without advancing."
            )
        select_radio_option(option, question, answer)

    print(
        "Answered the three recognized Dice application questions; "
        "continuing to the next step.",
        flush=True,
    )
    click_exact_button(page, "Next", "Next")


def click_submit_or_answer_questions(page):
    submit_button = page.get_by_role("button", name="Submit", exact=True)
    try:
        submit_button.wait_for(state="visible", timeout=3_000)
    except PlaywrightTimeoutError:
        answer_known_application_questions(page)
        submit_button = page.get_by_role("button", name="Submit", exact=True)
        try:
            submit_button.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError as error:
            raise RuntimeError(
                "The recognized application questions were answered, but Dice "
                "did not show the Submit button on the next step."
            ) from error
    submit_button.click()


def wait_for_application_success(page):
    try:
        page.wait_for_function(
            """() => /\\/wizard\\/success(?:[?#]|$)/i.test(
                    location.pathname + location.search + location.hash
                ) || /application is on its way/i.test(document.body.innerText)""",
            timeout=30_000,
        )
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Dice did not show an application success page after Submit. "
            f"Current page: {page.url!r}. Check Applied Jobs before retrying."
        ) from error


def return_to_search(page):
    print(f"Returning to the filtered search: {SEARCH_URL}", flush=True)
    try:
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
    except PlaywrightError as error:
        raise RuntimeError(
            "The application was submitted, but Dice search could not be "
            "reloaded. Rerun the command to continue with remaining jobs."
        ) from error
    accept_cookies_if_present(page)
    wait_for_search_results(page)


def apply_to_job(page, job):
    page.goto(job["url"], wait_until="domcontentloaded")
    click_apply_action(page)
    click_exact_button(page, "Next", "Next")
    click_submit_or_answer_questions(page)
    wait_for_application_success(page)


def main():
    parser = argparse.ArgumentParser(
        description="Use a dedicated Chrome profile for Dice Easy Apply results."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply to matching results and return to the search after each success.",
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
            "Opening the dedicated Dice Chrome profile "
            f"at {CHROME_PROFILE_DIR}...",
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
                "Could not start Chrome with the dedicated Dice profile. "
                "Close any Chrome window opened by another Dice runner and retry."
            ) from error
        page = context.pages[0] if context.pages else context.new_page()
        print(f"Navigating to {SEARCH_URL}", flush=True)
        try:
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError as error:
            raise RuntimeError(
                f"Chrome opened, but navigation to the Dice search failed "
                f"({page.url!r}). Check your connection and Chrome window."
            ) from error
        accept_cookies_if_present(page)
        wait_for_search_results(page)
        print(f"Dice search results loaded: {page.url}", flush=True)
        if args.apply:
            wait_for_sign_in(page)
            if not page.url.startswith("https://www.dice.com/jobs"):
                page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
                wait_for_search_results(page)

        jobs = find_jobs(page)
        pending = [
            job
            for job in jobs
            if job["id"] not in completed_ids
            and not re.search(r"\bApplied\b", job["card_text"], re.IGNORECASE)
        ]
        print(f"Dice reports {len(jobs)} matching results.")
        for job in jobs:
            status = "already processed" if job not in pending else "ready"
            print(f"- {job['id']}: {status}", flush=True)

        if not jobs:
            print(
                "No matching job listings loaded; no applications will be submitted.",
                flush=True,
            )
            context.close()
            return

        if not args.apply:
            print("\nPreview only. Run with --apply to start applying.")
            context.close()
            return

        print(
            f"Starting applications for {len(pending)} eligible result(s)...",
            flush=True,
        )

        applied_this_run = 0
        while pending:
            job = pending[0]
            print(f"\nApplying to Dice listing {job['id']}...")
            apply_to_job(page, job)
            state[job["id"]] = {
                "url": job["url"],
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
            write_state(state)
            completed_ids.add(job["id"])
            applied_this_run += 1
            print("Dice showed the success screen; application recorded locally.")
            return_to_search(page)
            jobs = find_jobs(page)
            pending = [
                candidate
                for candidate in jobs
                if candidate["id"] not in completed_ids
                and not re.search(
                    r"\bApplied\b", candidate["card_text"], re.IGNORECASE
                )
            ]

        print(f"\nFinished. Submitted {applied_this_run} application(s) in this run.")
        context.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        raise SystemExit(1) from error
