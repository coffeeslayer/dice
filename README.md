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

## LinkedIn Easy Apply runner

The LinkedIn runner uses the search URL you provided (`.net react remote`, Pensacola Metropolitan Area, past 7 days). It opens a separate Chrome profile at `%LOCALAPPDATA%\LinkedInJobSearch\ChromeProfile` and keeps the session for future runs. It can sign in using `LINKEDIN_EMAIL` and `LINKEDIN_PASSWORD` in the local, Git-ignored `.env` file, or you can sign in manually in Chrome. If LinkedIn requests verification, complete it in the browser. The email value must be the email address, phone number, or username accepted by LinkedIn.

Preview the current search results:

```powershell
python linkedin_apply_jobs.py
```

To start assisted applications:

```powershell
python linkedin_apply_jobs.py --apply
```

If the runner detects an **Additional Questions** page but finds none of its configured question fields, it closes the application, clicks **Discard** if LinkedIn asks whether to save it, and skips to the next job without submitting.

The runner selects each eligible listing in the left results pane, scrolling the results column to bring each job card into view, and checks its details pane for **Easy Apply**. It processes each search-results page and advances through LinkedIn's results pagination until no next page is available, skipping applications already recorded. A visible red cursor marker follows its listing and button clicks. It logs the target job ID and URL before selecting it. If the button is missing (for example, because you already applied), it leaves the card unchanged and proceeds to the next result; it does not click the **X**, which dismisses the recommendation and changes it to Undo. When the button is present, it opens the application flow and clicks available **Next** or **Review** buttons on its paginated screens, waiting for each page to appear. If the **What is your current location?** field is blank, it fills it with **Navarre, FL**; a value already present is left unchanged. On **Additional Questions**, it identifies fields by their accessible labels: enters **15** for the professional software engineering experience question, **12** for the Microsoft Azure experience question, and selects the radio inputs for the C#/.NET, client-facing technical consulting/pre-sales, and production software systems questions. It then clicks that dialog's **Next** button. On the **Review your application** page, it scrolls to and unchecks a visible follow-company checkbox if selected (or continues if there is none), then scrolls to **Submit application** and waits for LinkedIn's success message. For application dialogs without paginated steps, it detects **Submit application** in the **Apply to** dialog and scrolls the dialog to reveal it before clicking. If LinkedIn shows the post-submission resume/profile prompt, the runner clicks **Not now** before continuing. After LinkedIn confirms an application, the runner records it automatically and moves on without a terminal confirmation. If the runner cannot detect or advance an application step, it waits up to five minutes for LinkedIn's success message; complete any required manual steps in Chrome, and it will resume automatically after confirmation. If success remains unconfirmed after five minutes, it closes and discards the incomplete application when LinkedIn allows it, then continues to the next job without recording the skipped application. After all available results are processed or the runner stops, the browser stays open until you press Enter in the terminal so you can inspect it. Completed applications are stored in `.linkedin-application-state.json`, which is excluded from Git.
