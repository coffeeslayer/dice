# Dice Job Search

A local job-search dashboard with an optional Playwright runner for Dice Easy Apply. Open `index.html` in a browser to adjust the filters and open the Dice search. You can sign in directly in the runner's Chrome window or configure credentials in a local `.env` file.

The default search matches:

- Keywords: `.net developer with react`
- Posted: past 3 days
- Workplace: remote
- Easy Apply: enabled

The job tracker stores listing links, status, and notes in the browser's local storage on the current device.

## Search dashboard

Open `index.html` in a modern browser. No installation or server is required.

## Playwright Easy Apply runner

The runner opens an isolated Chrome profile at `%LOCALAPPDATA%\DiceJobSearch\ChromeProfile` and navigates to the filtered Dice search. This separate profile is required because current Chrome versions block Playwright's remote-debugging connection to your regular profile. It cannot inherit your regular Chrome login. If the dedicated profile is signed out, the runner uses the optional local `.env` credentials to sign in, or waits for you to sign in directly in that Chrome window. Chrome keeps the session for future runs. After submitting an application, it detects Dice's success page, returns directly to the original filtered search URL, reloads the listings, and continues with the next unapplied result. It records successful applications locally and skips jobs already recorded or marked Applied in the result list.

From this folder, install the runner dependency once:

```powershell
python -m pip install -r requirements.txt
```

For automatic email/password sign-in, fill in `DICE_EMAIL` and `DICE_PASSWORD` in the local `.env` file. The `.env` file is excluded from Git. The runner submits these values to Dice's sign-in form and does not print them. The file is plain text, so protect it and do not share or commit it. Leave both values empty to sign in manually. If Dice requests a one-time code or another verification step, complete it in Chrome.

Preview the results without applying:

```powershell
python apply_jobs.py
```

To run the application flow:

```powershell
python apply_jobs.py --apply
```

The runner prints progress while opening Chrome and loading the search. It clicks **Accept all** on Dice's cookie prompt when available; if the prompt is missing or cannot be clicked, it continues. If the application has no **Submit** button but shows the three recognized questions (C2C/1099, recruiter representation, and US work authorization), it selects **No**, **No**, and **Yes**, then clicks **Next**. It locates each answer inside its question group and clicks the radio without scrolling the page; unexpected radio markup is reported and stops the application. Running `python apply_jobs.py --apply` automatically submits each eligible result without a further confirmation prompt; if the dedicated Chrome profile is signed out, the runner waits for you to sign in there first. Running without `--apply` only previews results. If Dice has no matching results, the runner exits without submitting anything. The runner stops if Dice's page or expected buttons change; it does not retry an uncertain submission. If it stops after clicking Submit, check Dice's Applied Jobs before running it again.

The dedicated Chrome profile is stored under your Windows local app data, outside this project. The application history is stored in `.dice-application-state.json` and excluded from Git.
