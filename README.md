# TradeLab frontend

TradeLab teaches beginners how the stock market works: interactive lessons, paper trading with virtual money, a portfolio view, a trading journal and a tutor. This repository is the frontend plus a small **Flask backend** that serves the site, handles sign-up, sign-in and guest limits (saved in a SQLite database), delivers the account-only lessons, and answers tutor questions with a local Qwen model. Every price is simulated and every dollar is virtual.

Built with HTML, CSS and vanilla JavaScript (ES modules). Chart.js is the only third-party frontend code, and it is vendored. The backend's Python dependencies are listed in `requirements.txt`; the account code itself (`server/`) uses only Python's standard library.

## Run it locally

### The demo on a Mac: double-click `Start TradeLab Demo.command`

It runs the whole app (website, sign-in, guest limits and the Qwen tutor) on TradeLab's production server at http://127.0.0.1:5001:

1. The first time, it installs the server's packages into `~/.tradelab-demo/venv` (nothing else on the Mac changes). It does this again after `requirements.txt` changes, which needs internet once.
2. It finds the computer running llama-server on your network: any computer answering on port 8080, or the address saved in `~/.tradelab-demo/model-url.txt`. That computer must start llama-server with `--host 0.0.0.0`, and both computers must be on the same Wi-Fi.
3. It checks that the model answers, then opens the site. Leave its window open during the demo; Control-C or closing the window stops TradeLab.

The same thing by hand, from this folder:

```bash
python3 -m pip install -r requirements.txt
python3 serve.py                                   # http://localhost:5001
python3 serve.py --model-url 192.168.1.23:8080     # when the model runs on another computer
```

On Windows (PowerShell), with the project's virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python serve.py
```

Open http://127.0.0.1:5001 (port 5001, because macOS uses 5000 for AirPlay). To use the AI tutor, also start `llama-server` with your local Qwen GGUF model on port 8080; see [docs/integration.md](docs/integration.md) for the command. The account database is `tradelab.db` in this folder, created on first run; set `TRADELAB_DB` to use a different file. While changing the backend, Flask's development server works too: `python -m flask --app backend.app run --host 127.0.0.1 --port 5000 --debug` (on a Mac, use port 5001).

Keep `dataSource: 'mock'` in `js/config.js`; `tutorApiEnabled: true` is the default and enables the Flask/Qwen tutor. Set it to `false` for prewritten tutor answers.

### HTTPS and hosting

`serve.py` is TradeLab's production server ([Hypercorn](https://hypercorn.readthedocs.io/), on macOS, Linux and Windows). It handles HTTPS itself, adds security headers and rate limits sign-in, sign-up and the tutor. [docs/deployment.md](docs/deployment.md) has the steps for each setup:

```bash
python3 serve.py --https --host 0.0.0.0     # HTTPS for phones and laptops on your Wi-Fi: https://<this computer>:8443
python3 serve.py --behind-proxy             # behind Caddy on a server with a domain (deploy/Caddyfile, deploy/tradelab.service)
```

With `--https` alone, it makes a certificate for this computer, signed by a certificate authority it creates in `~/.tradelab/certs`. Browsers warn until you trust that authority on each device; the guide shows how.

### Without the Qwen tutor

Neither of these servers needs any packages:

```bash
# Python 3.8 or newer: the whole app, including accounts, guest limits and every lesson
python3 scripts/dev_server.py    # http://127.0.0.1:5173

# Node 18 or newer: the frontend only, without accounts (frontend work only)
npm run dev                      # http://127.0.0.1:5173
```

With either one, turn off the local tutor in `js/config.js` first, so the tutor uses its prewritten answers:

```js
tutorApiEnabled: false,
```

`npm run server` starts the Python account server too. Use another port with `python3 scripts/dev_server.py 8000` or `npm run dev -- 8000`. Both servers send the right MIME type for `.js` files (some Windows setups otherwise break module loading) and disable caching so edits show up on refresh. On Windows, type `python` instead of `python3`.

Flask and the Python server save accounts in the same `tradelab.db` in this folder, and create the file on first run. When you use the Node server, the Account page says that accounts need the TradeLab server.

### Tests

The Node tests and the account server tests need nothing installed; the tutor backend tests need the packages in `requirements.txt`:

```bash
npm test                                   # business rules, sign-up rules and the tutor frontend (Node)
python3 -m unittest discover -s tests -v   # account server, tutor backend and knowledge library (Python)
```

## What you get on first launch

The first visit creates a demo account with $10,000 in virtual cash, five example trades and two example journal entries, all labeled **Example**, sitting on simulated day 15. That way the dashboard, portfolio and journal show what they do right away. To start clean, use **Start with an empty account** on the Overview banner or **Settings > Reset simulated account**.

Prices only change when you press **Advance 1 day** or **Advance 1 week**. That keeps the simulation honest (nothing pretends to be live) and lets you see how a decision plays out.

## Guest access

Guests can explore the app, with server-enforced limits:

- **Simulation:** 21 simulated trading-day advances (one virtual month), starting at the existing demo clock's day 15 and ending at day 36. An advance-week request at the boundary is shortened to the remaining days.
- **Courses:** the three beginner courses *What Is a Stock?*, *How Prices Move*, and *Market vs. Limit Orders*. Other lesson bodies are served only by Flask after it verifies the account session.
- **Tutor:** requires a signed-in account, for both local Qwen and preview mode.
- **Journal:** two new entries per guest session. Preloaded `Example` entries are sample data and do not consume those creation slots. Deleting an entry does not restore a slot.

The limits are kept in SQLite against a random, HttpOnly guest-session cookie that expires after 365 days; progress and journal text remain in this browser's local storage until an account takes them over. Clearing site cookies creates a new anonymous identity and resets its guest allowance, so these controls prevent ordinary refreshes and client-side counter edits but are not a substitute for verified accounts or abuse controls on a public deployment.

## Pages

| Page | What it does |
| --- | --- |
| Overview | Cash, portfolio value, today's simulated P/L, learning progress, a price chart with time ranges, the watchlist with sparklines, recent simulated trades, suggested lessons and a way into the tutor. |
| Learn | The course outline: 10 lessons in 7 topics with difficulty, time, status and progress, filterable by topic, level and status. |
| Lesson | One section at a time with section navigation, figures, three interactive tools (candle builder, limit-order fill checker, position-size calculator) and a knowledge check. Passing two of three completes the lesson. |
| Practice Trading | Searchable stock list with a watchlist, quote and statistics, line or candlestick chart across 1D to 5Y, an order ticket with live validation and a concentration warning, a confirmation step, your position and the order history. |
| Portfolio | Totals, unrealized and realized P/L, account value over simulated days, allocation by holding or sector, holdings and the full transaction history. |
| Trading Journal | Entries with symbol, entry and exit price, shares, planned stop and target, thesis, risks and lessons. Create, edit and delete. Insights score planning and follow-through first; outcome statistics appear after three closed entries. |
| AI Tutor | Chat with suggested questions, a loading state, formatted answers and a clear-conversation control. Uses local Qwen by default, with a switch to prewritten mock answers. |
| Settings | Theme (dark, light, system), gain and loss colors (green/red or blue/orange), density, reduced motion, default chart style and range, learning level, account reset and clearing local data. |
| Account | Create an account (username, email, password) or sign in, with live checks and a status message for every result. Reached from **Sign up** in the header or the profile menu. |

Search with **Ctrl+K**, **Cmd+K** or **/** to jump to any page, simulated stock or lesson.

## Project structure

```
tradelab/
  index.html                 App shell, early theme script, script and style tags
  package.json               npm scripts (dev, test); no dependencies
  requirements.txt           Flask, requests and pypdf for the backend; Hypercorn and cryptography for serve.py
  serve.py                   Production server: HTTPS, HTTP/2, proxy support, access log (docs/deployment.md)
  ai_system_prompt.txt       Local Qwen tutor behavior and safety instructions
  backend/
    app.py                   Flask server: the site, accounts, guest limits, lessons and the tutor
    security.py              Security headers, Content-Security-Policy, rate limits, reverse-proxy support
  assets/
    icons/favicon.svg
    fonts/                   Archivo variable font (self-hosted) + license
    vendor/                  Chart.js 4.5.1 UMD build + license
  css/
    tokens.css               Design tokens: color (dark first + light), type, space, motion
    base.css                 Reset, typography, focus styles, utilities
    layout.css               Shell, sidebar, header, page grid, panels
    components.css           Buttons, controls, pills, tables, dialogs, toasts, charts
    pages.css                Page-specific layout
    responsive.css           Breakpoints: 1600, 1280, 1100, 900 (drawer), 640, 480
  js/
    app.js                   Boot: store, preferences, shell, market data, router
    config.js                Data source switch (mock or api) and app settings
    router.js                Hash router and route table
    state.js                 Central store, persistence, derived selectors
    storage.js               localStorage wrapper with in-memory fallback
    core/                    Pure business logic (no DOM): portfolio, orders, progress,
                             journal, accounts, calendar, seeded random numbers
    data/                    Mock data: market model, lessons, tutor answers, example account
    services/                The only layer that reads or writes domain data; each has a
                             mock adapter and a remote (Flask) adapter
    components/              Shell, charts, dialogs, notifications, search, icons, widgets
    pages/                   One module per page: mount(root, ctx) returns a cleanup
    utils/                   Safe HTML templating, formatting, a small Markdown renderer
  docs/
    integration.md           Flask tutor setup and future API contracts
    deployment.md            Running in production: HTTPS on your network, a domain with Caddy, hosting platforms
    mobile.md                What carries over to a mobile app and what doesn't
  server/accounts.py         Sign-up rules, password hashing, the SQLite user database, saved progress
  server/lesson_content.json Full course content delivered by Flask after access checks
  server/email_check.py      Email checker: provider typos, reserved domains, DNS MX lookup
  scripts/                   Zero-dependency servers: dev_server.py (app + accounts), dev-server.mjs (app only)
  Start TradeLab Demo.command  Mac launcher for the demo: installs, finds the model, starts serve.py
  deploy/                    Caddyfile (automatic HTTPS for a domain) and tradelab.service (Linux service)
  tests/
    core.test.mjs            Unit tests for core/, the mock data and the sign-up rules
    guest-limits.test.mjs    Guest limits in the frontend: simulation days and journal entries come from the server
    tutor-*.test.mjs         Tutor frontend: mock replies, the local API client, streaming
    typewriter.test.mjs      Typewriter animation for prewritten replies
    test_accounts.py         Unit tests for the account server and database
    test_flask_accounts.py   The account API on Flask: progress, origins, sizes, private files
    test_security.py         Security headers, the page's Content-Security-Policy, rate limits
    test_production.py       serve.py over real HTTPS: accounts, streaming tutor, redirects, proxies, certificates
    test_backend.py          Flask tutor, accounts, guest limits, lessons and static files
    test_knowledge_base.py   Knowledge library tests
    account-cases.json       Sign-up cases both test suites check, so browser and server agree
  tradelab.db                The user database (created by the Python server; not in git)
```

Icons are drawn in `js/components/icons.js` (one consistent 24px set) rather than as separate files, so they inherit color from the theme.

## How it fits together

**One source of truth.** The account is stored as a transaction ledger. Cash, holdings, average cost and realized P/L are always derived from it by `core/portfolio.js`, so no copy of a balance can drift. Lesson progress, journal entries, the tutor conversation and preferences live in the same store (`state.js`).

**Services decide where data comes from.** Pages never import mock data for domain state. They call services (`marketDataService`, `tradingService`, `progressService`, `journalService`, `tutorService`). Non-tutor services pick an adapter based on `config.dataSource`:

- `mock` (default): runs locally against the store and the simulated market.
- `api`: calls the Flask backend through `services/apiClient.js`.

The tutor adapter is selected independently from `dataSource`, so enabling its local API does not change the source of market, account, progress, or journal data.

**Business rules are pure functions.** Order validation, fills, P/L, day change, the equity curve, quiz grading, lesson progress, journal validation and journal statistics live in `js/core/` with no DOM or storage access. They are covered by `npm test` and are written to be ported to Python as-is.

**Rendering is plain DOM.** Pages build markup with an `html` tagged template that escapes every value by default (`utils/dom.js`), use event delegation, and re-render only the regions whose store slice changed (`watch()`).

## Accounts and the user database

Sign up on the **Account** page (header: **Sign up**, or the profile menu). The form asks for:

- a **username**: no spaces or @, up to 30 characters, unique (capital letters don't count, so Ayoub and ayoub are the same)
- an **email** address that can actually receive mail (stored in lowercase, unique). See "The email checker" below.
- a **password** of at least 8 characters with at least one capital letter and one special character such as ! @ # or $

A checklist under the password field ticks off each requirement as you type, and a line under the email field shows what the email checker found. When you submit, a status banner at the top of the form shows the result: **Account created** in green with a check mark, or **Account not created** in red, listing each problem with a link to the field. The same messages appear under the fields. Sign in with your username or email; signing in and out shows a status too.

### The email checker

When you pause typing an email, and again when you submit, the server checks (`server/email_check.py`, standard library only):

1. **Typos of popular providers.** gnail.com, gmial.com, hotmial.com, yahooo.com, outlok.com and similar get "Did you mean …@gmail.com?" with a button that fixes the address. These typo domains usually exist and even accept mail (typo-squatters register them), so a DNS lookup alone would let them through.
2. **Reserved and made-up endings.** example.com, .test and .invalid can't receive mail; endings like .con or .ed get "Did you mean .com / .edu?".
3. **The domain's mail servers.** A DNS lookup for MX records rejects domains that don't exist, have no mail server, or publish a "null MX" (meaning they never accept mail).

The lookup uses the DNS servers in `/etc/resolv.conf`, then 1.1.1.1 and 8.8.8.8, and caches answers for ten minutes. If no DNS server answers (you're offline), the address is accepted as unverified so sign-up still works. Set `TRADELAB_DNS_SERVER` to use a specific DNS server. Try the checker on its own:

```bash
python3 server/email_check.py you@gmail.com someone@gnail.com
```

This proves the address's domain can receive email, not that the mailbox exists or belongs to the person signing up. That takes a confirmation email, which needs an email account (SMTP) for the server to send from.

### Progress belongs to each account

Signed in, everything you do (lessons, simulated trades and the simulated day, journal entries, the tutor conversation, notifications and settings) is saved to your account in the `user_data` table and loads again whenever you sign in, in any browser. Changes save about a second after you make them; the Account page shows "Progress saved to your account".

Signed out, TradeLab shows a separate **guest** copy kept in this browser, so one person's progress never shows for another. When an account has nothing saved yet (a new sign-up, or an account from before this feature), it takes over the progress in this browser, and the guest copy starts fresh.

How it works:

- The browser checks the rules first (`js/core/accounts.js`), and the server checks them again (`server/accounts.py`) and has the final say. Both use the same messages, and `tests/account-cases.json` is run against both.
- Accounts are saved in the `users` table of `tradelab.db`. Passwords are never stored: the server keeps a salted PBKDF2-SHA256 hash (1,000,000 iterations) in Werkzeug's format, so a Flask backend can check them with `werkzeug.security.check_password_hash`.
- If `tradelab.db` already has a `users` table (`id`, `display_name`, `email`, `password_hash`, `created_at`), the server adds a `username` column and leaves existing rows alone. Those accounts sign in with their email. New accounts get the username in both `username` and `display_name`.
- Signing in sets an HttpOnly, SameSite=Lax session cookie that lasts 7 days. The `sessions` table stores only a hash of each session token.
- The server refuses requests from other websites that would change anything (signing up, in or out, or saving progress), and never serves the database, its own code or dotfiles such as `.git`.
- On Flask, too many sign-ins, sign-ups or tutor questions from one network get "wait a few minutes" (`429`), so passwords can't be guessed quickly. The limits are in [docs/deployment.md](docs/deployment.md#security).
- **Settings > Start over** (signed in) resets your account's progress; **Clear all local demo data** (signed out) resets the guest copy. Neither deletes accounts.

See every account (never the passwords), and when each one last saved progress, from this folder with:

```bash
python3 -c "import sqlite3; [print(row) for row in sqlite3.connect('tradelab.db').execute('SELECT u.id, u.username, u.email, u.created_at, d.updated_at FROM users u LEFT JOIN user_data d ON d.user_id = u.id')]"
```

The account API (`POST /api/users`, `POST`/`GET`/`DELETE /api/session`, `POST /api/email-check`, `GET`/`PUT /api/me/data`) is described in [docs/integration.md](docs/integration.md#accounts).

## Mock data

| What | Where | Notes |
| --- | --- | --- |
| Eight fictional instruments and their prices | `js/data/mockMarketData.js` | "TradeLab Simulated Market": a seeded random walk with market, sector and company factors, volatility regimes and quarterly jumps. Deterministic, so every device sees the same history. About five years of history and five years of future days. |
| Intraday bars (1D, 1W) | same file | Brownian bridge from each day's open to close, stretched to its high and low. |
| Example account and journal | `js/data/exampleAccount.js` | Labeled Example everywhere; removed by Reset. |
| Lessons and quizzes | `js/data/mockLessons.js` | Educational content, not financial advice. |
| Tutor answers | `js/data/mockTutorResponses.js` | Keyword topic matching to prewritten Markdown when `tutorApiEnabled` is false. |

Simulated dates are weekdays only (no market holidays). The companies don't exist, and the prices don't track anything real.

## Local Qwen tutor

The tutor uses the local Flask and llama.cpp backend by default. All other
application services remain on mock data (`dataSource: 'mock'`). See
[docs/integration.md](docs/integration.md) for the Windows PowerShell setup,
endpoint test command, and how to switch the tutor back to prewritten mock
answers with `tutorApiEnabled: false`.

## Local financial knowledge library (RAG)

The tutor uses a local BM25-style text index to retrieve relevant excerpts
before asking Qwen to answer. This is retrieval-augmented generation (RAG),
**not fine-tuning**: it does not train or change the Qwen model. The index,
imported text, and chat requests stay local. No source corpus is bundled;
Qwen is not called for a question when no relevant indexed material is found.

### Add a permitted source

Activate the existing project environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Import a local `.txt`, `.md`, `.html`, `.htm`, or text-based `.pdf` file.
Provide its title and publisher, its original URL when it has one, and a
specific license or permission basis you have verified:

```powershell
python -m backend.ingest_knowledge add-file "C:\path\to\permitted-material.pdf" `
  --title "Investor Education: Understanding Risk" `
  --topic "Investment risk and diversification" `
  --publisher "Publisher name shown on the document" `
  --url "https://publisher.example/education/understanding-risk" `
  --publication-date "2025-02-01" `
  --rights-basis "Verified CC BY 4.0 license permits local storage and educational use" `
  --confirm-rights
```

For example, use the actual license and scope printed on the work, or describe
the written permission you obtained, including who granted it and what uses it
covers. Do not use a generic statement such as “I have permission” without
verifying the basis. `--confirm-rights` is your attestation, not legal
verification by the importer. The `--url` value is recorded for attribution
only; `add-file` reads the specified local file and does not fetch that URL.
PDFs must contain extractable text; scanned-image PDFs are not OCR-processed.

Or fetch one explicitly selected page at a time from the allowlisted HTTPS
hosts Investor.gov, FINRA, or SEC:

```powershell
python -m backend.ingest_knowledge add-url `
  --url "https://www.investor.gov/REPLACE-WITH-A-VERIFIED-PAGE-URL" `
  --topic "Stock market fundamentals" `
  --rights-basis "Describe the permission or license you verified" `
  --confirm-rights
```

The URL importer checks `robots.txt`, refuses disallowed paths and
cross-domain redirects, sends a descriptive user-agent, limits downloads, and
does not crawl links. If `robots.txt` returns HTTP 403 or another response
that prevents checking its rules, permission is **unresolved**: the importer
does not fetch the page. `--confirm-rights` cannot override that check or
authorize web scraping. Use `add-file` only when you already have a local
copy and have independently verified you may store and use it. The importer
does **not** determine copyright permission or verify claims, even when
`--confirm-rights` is given. Local source claims and rights details are
recorded as user-reported and not independently verified.

Choose one of these topic labels exactly:

- `Stock market fundamentals`
- `Stocks, bonds, ETFs, and index funds`
- `Financial statements and basic valuation`
- `Investment risk and diversification`
- `Market orders and limit orders`
- `Candlestick charts and common trading terminology`
- `Paper trading and portfolio management`

### Rebuild and inspect the index

After adding or replacing source files, rebuild the index and inspect its
source list:

```powershell
python -m backend.ingest_knowledge rebuild
python -m backend.ingest_knowledge list
```

Rebuild whenever source records change. Imported text under
`knowledge_base\sources\` and the derived `knowledge_base\index.json` are
git-ignored to avoid accidentally committing material with usage restrictions.
Keep backups of any local source files you need; they are not tracked in git.
The retrieval index can always be regenerated from those source records.
Rebuilding replaces the index from the source-record files; it does not append
duplicate documents.

### Search and verify retrieval

Search the rebuilt local index without calling Qwen:

```powershell
python -m backend.ingest_knowledge search "How does diversification reduce investment risk?"
```

Each result shows its passage, source title, publisher, original URL, and
lexical relevance score. An unrelated question should return “No relevant
passages found.” Search runs locally and uses the same retriever as the tutor.

To verify that Qwen is using retrieved evidence, first make sure the local
`llama-server` is running on `127.0.0.1:8080` and Flask is running on
`127.0.0.1:5000`. Search for the question above and confirm the SEC guide is
among the results. Then send the same question to the Flask endpoint:

```powershell
$body = @{
  messages = @(@{
    role = "user"
    content = "Explain how diversification can reduce investment risk, and identify what it cannot guarantee."
  })
  context = @{ level = "beginner"; route = "tutor" }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/tutor/chat" `
  -Method Post -ContentType "application/json" -Body $body
```

The tutor's response should describe diversification as potentially reducing
some risk, not guaranteeing profit or eliminating all losses. Source passages
are used internally for grounding; replies no longer append source lists or
quote the source text. The backend caps retrieval at three passages and 2,800 retrieval-context
characters, limits recent conversation history to 2,500 characters while
retaining the current question, sends the ordered recent exchange to Qwen, and treats passages as untrusted reference
data. Qwen3's supported `enable_thinking` template option is disabled for these
short tutor answers so the local model's context budget is used for the answer.
When a short follow-up has no standalone topic, retrieval uses the recent
conversation to resolve its subject. If no passage matches, Qwen may still
answer stable general educational questions, with instructions to disclose
that no verified local source was retrieved; it must not invent citations or
current/source-specific facts. Index-read failures remain distinct backend
errors. Simple greetings and direct questions about the tutor's identity are
answered without retrieval.

### Test and troubleshoot

Run the existing frontend tests and local knowledge/backend tests:

```powershell
npm test
python -m unittest discover -s tests -p "test_*.py" -v
```

- **Verify the local pipeline**: use the `search` command above; automated
  backend tests build temporary source records and mock Qwen while asserting
  the retrieved passage and source URL reach the model context.
- **“No relevant, indexed source”**: import a permitted document tagged with
  the appropriate topic, run `rebuild`, and ask a question addressed by its
  text. The tutor deliberately will not ask Qwen to answer unsupported
  factual questions.
- **“Could not read the local tutor knowledge index”**: run the rebuild
  command again; if it fails, inspect invalid or unreadable JSON records in
  `knowledge_base\sources\`.
- **URL import refused**: verify the URL uses HTTPS and an allowlisted
  Investor.gov, FINRA, or SEC host, review that site's current terms, and
  check its `robots.txt`. The pipeline will not bypass access restrictions.
- **Weak/missed match**: use a more specific question and check that the
  imported text contains the relevant terms. Retrieval is lexical (BM25),
  not semantic vector search; it is a simple local index, not a guarantee of
  authority or completeness.

## Connecting the other services to a backend later

See [docs/integration.md](docs/integration.md) for the remaining endpoint list and request/response shapes. Only switch `dataSource` to `'api'` after implementing those market, account, lesson-progress, and journal endpoints.

## Accessibility

- Semantic landmarks, a skip link, one `h1` per page and announced page changes.
- Visible focus on every control; dialogs trap focus and return it on close; the mobile drawer makes the page behind it inert.
- Gains and losses always carry a sign and an arrow, never color alone; up candles are hollow and down candles filled; a color-vision-friendly palette is one setting away.
- Every chart has a text summary for screen readers and a "Show as a table" view.
- Touch screens get 44px targets; inputs use 16px text on touch devices so iOS doesn't zoom.
- `prefers-reduced-motion` is respected, and there is an in-app setting too.

## Known limitations

- **Guest data is browser-only.** Signed out, progress lives in this browser's `localStorage`, which is not a secure or permanent store; clearing site data resets it. Signed in, it's saved to your account on the server.
- **Guest limits use a server cookie.** The SQLite database stores only a hash of the random guest token and its simulation/journal counters; it does not store guest journal text or simulated trades. The cookie lasts 365 days, but clearing it starts a new anonymous session and allowance.
- **Course content is protected at delivery.** The three guest lessons are public frontend content; other lesson bodies live in `server/lesson_content.json` and are returned only to a signed-in account by Flask. Catalog titles and summaries remain public.
- **No confirmation emails or password reset.** The email checker confirms the domain receives mail, not the mailbox. Sign-in, sign-up, the email checker and the tutor are rate limited on Flask (`serve.py`), but not on `scripts/dev_server.py`, which is for development only.
- **One server for the full local experience.** Flask serves the frontend, local tutor API, guest limits, and SQLite-backed sign-in and account progress. The standard-library account server is still available if you do not need the tutor.
- **Market orders only.** Limit and stop orders, fractional shares, short selling, fees, dividends and a bid/ask spread are not simulated yet. The order lesson says so.
- **Other backend services.** The market, trading, progress and journal API endpoints are not implemented; those features run on mock data, saved to your account when you're signed in.
- **Tutor preview.** With the local tutor off, the tutor answers a fixed set of prewritten topics; anything else gets an honest "I don't have an answer for that yet".
- **One tab at a time.** Two open tabs don't sync with each other; signed in, the last tab to save wins.
- **No market holidays** in the simulated calendar.
- **English only**, US dollar formatting.

## Licenses

- Chart.js 4.5.1, MIT (`assets/vendor/CHART_JS_LICENSE.md`)
- Archivo, SIL Open Font License 1.1 (`assets/fonts/ARCHIVO_OFL.txt`)
