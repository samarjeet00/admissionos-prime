# AdmissionOS Prime — Telegram / WhatsApp bot

Private admission-intelligence bot for Parul University. Enrolled people message it on Telegram or WhatsApp;
it reasons over live admissions-portal (PUAP) data as **AdmissionOS Prime** and replies with executive-format
analysis — Daily Intelligence Report with an Admission Readiness Score, Deadline Optimizer, forecasts, lead /
source / event / competitor intelligence. No website.

```
Telegram (long polling) ─┐
                         ├─▶ engine ──▶ Claude Opus 5 (AdmissionOS Prime prompt + tier-scoped tools)
WhatsApp (Cloud API      │                        │ tool calls
 webhook, HTTPS) ────────┘                        ▼
                                          PUAP MCP server ──▶ admissions.paruluniversity.ac.in (read-only)
```

## 1. Setup (Windows - double-click or run in CMD)

Prerequisite: Python 3.12+ from python.org with "Add python.exe to PATH" ticked, and Git.

```bat
git clone https://github.com/samarjeetsingh45296-hue/admissionos-prime.git
cd admissionos-prime
setup.bat
```

`setup.bat` creates the virtual environment, installs packages, copies `.env.example` to `.env` and opens it in
Notepad. Fill in these and save:

| `.env` variable | What to put |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (console.anthropic.com → API Keys). |
| `PUAP_MCP_URL` (+ `PUAP_MCP_TOKEN`) | Streamable-HTTP URL of the PUAP MCP server — the same endpoint added to Claude as the admissions connector (it runs from `/opt/puap-mcp` on its host). Or `PUAP_MCP_COMMAND` to launch it locally over stdio. |
| `TELEGRAM_BOT_TOKEN` | From **@BotFather** → `/newbot`. Long polling — works from any machine, no public URL. |
| `WHATSAPP_*` | From **Meta for Developers** → your app → WhatsApp (see §3). Needs a public HTTPS URL. Leave empty to run Telegram only. |

Start the bot:

```bat
run.bat
```

Leave that window open (Ctrl+C stops it). Linux / macOS: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cp .env.example .env && .venv/bin/python -m app.main`.

## 2. Access control - who can use the bot

Nobody gets an answer until they are enrolled. Two ways in:

**Ask-and-approve (default).** An unknown person messages the bot and gets "your access request has been sent to the
administrator". Every administrator (`"admin": true` in `data/users.json`) receives a Telegram message with buttons
**Viewer / Operational / Executive / Deny**. One tap enrols the person at that level and notifies them; Deny notifies
them too. WhatsApp requests arrive the same way, in the administrator's Telegram. Pending requests are kept in
`data/access_requests.json`; repeat messages remind at most once an hour.

**Manual enrolment.** From a second CMD window in the project folder:

```bat
users.bat add samar --name "Samar" --tier executive --telegram 123456789 --admin
users.bat set ravi --tier operational
users.bat list
users.bat disable ravi
```

`--admin` marks who receives the approval pop-ups (if nobody is flagged, all executives do). Changes apply on the
person's next message - no restart.

Why no password: on WhatsApp the phone number is SIM-bound and on Telegram the account is protected by the
platform; a password typed into the chat would just sit in the message history.
Only private (1-to-1) chats are answered — group messages are ignored so nobody's data leaks into a group.

| Tier | Intended for | Gets |
|---|---|---|
| `executive` | Management, Admission Admin, MIS | Every read-only tool: funnel, leads, sources, payments / fee collection, MIS workbook, exports, audit, call-centre, RBAC listings |
| `operational` | Admission Cell Admin, team leads, campaign managers | Funnel, leads, sources, campaigns, datasets / counts / queries, dashboards, call-centre health |
| `viewer` | Headline numbers only | Dashboard snapshot, today's registrations, weekly funnel, count reports |

Tool sets live in `app/brain.py::TIER_ALLOW`. **`counselor_conversion` and individual counselor performance
are removed from the brain** (leadership direction, 10 Sep 2026): denied at the tool layer and refused in the prompt.

## 3. WhatsApp Cloud API setup

1. Meta for Developers → Create app (Business) → add **WhatsApp**. Note the **Phone number ID** and create a
   permanent **System User token** (Business Settings → System users → Generate token, permission
   `whatsapp_business_messaging`). Put both in `.env`; copy the **App secret** to `WHATSAPP_APP_SECRET`.
2. Make `http://<this-machine>:8765/webhook` reachable over HTTPS — a reverse proxy on a server, or while
   testing a tunnel: `cloudflared tunnel --url http://localhost:8765`.
3. WhatsApp → Configuration → Webhook: callback URL `https://<public-host>/webhook`, verify token = your
   `WHATSAPP_VERIFY_TOKEN`; subscribe to the `messages` field.
4. Test numbers work immediately; for real numbers register your business phone number and complete
   Business Verification. Replies to a user's message are free-form inside the 24-hour customer-service
   window, which is exactly how a chatbot is used.

## 4. Commands

Type them in the chat (Telegram shows them in the `/` menu). Anything after a command becomes a focus.

| Command | Delivers |
|---|---|
| `/daily` | Daily Intelligence Report — Admission Readiness Score (0–100) with component table, opportunities, risks, actions, priorities |
| `/readiness` | Just the score and its components |
| `/deadline` | Deadline Optimizer — recommended close date, confidence, impact, risks, backup date |
| `/forecast` | Applications, admissions, fee collection — 30 / 60 / 90 days, low / base / high |
| `/leads` | Lead inflow, lead health, backlog, team-level calling strategy |
| `/sources` | Source & campaign performance, where to shift spend |
| `/events` | Exams, results, festivals, holidays in the next 60 days with impact ratings |
| `/competitors` | Competitor windows, scholarships, likely moves, our counter-moves |
| `/program <name>` | Full deep-dive for one programme |
| `/cancel`, `/reset`, `/reload`, `/help` | Stop the running request / fresh conversation / re-read the reference sheet / list commands |

Plain-English questions work too ("why did B.Pharm applications drop this week?"). Command prompts are in
`app/brain.py::COMMANDS`.

## 5. Institutional memory

`data/memory/*.md` is loaded into the model's context on every request (filename order): academic calendar,
festival calendar with impact ratings, playbook weights, intake targets, scholarship windows, competitor set.
Edit the markdown — no restart. Dates tagged `[verify]` are typical windows not yet confirmed against an official
notification; the model flags them when they drive a recommendation. Change the tag to `[verified]` once confirmed.

## 5b. The reference workbook (Google Sheet, read-only)

Three tabs of "Date Wise Performance Comparision - CCC" are always in the model's context - **Board Exam &
Result Dates**, **Entrance Exam & Result Dates** and **Last Dates Performance** (every past deadline and the
registrations it produced). The other seven tabs (day-wise Domestic / Goa / Online registrations and admissions,
CCC yield) are read on demand through the `reference_sheet_tab` tool, so the bot can compare any date or month
across sessions. The integration cannot write: it authenticates as a Google **service account** with the
`spreadsheets.readonly` scope only.

## 5c. Web verification, answer structure, deadline briefs

- **Internet is the authority for dates.** The bot has keyless `web_search` and `fetch_url` tools and a hard rule:
  any exam, result, notification, counselling, festival or holiday date is checked on the official site before
  it is used, even if memory has a value. Every number and date carries a source label - `[web: domain]`,
  `[sheet]`, `[portal]`, `[memory]` or `[estimate]`.
- **Every answer is structured.** Strategy questions use the executive format; factual lookups use the compact
  Answer / Why it matters / Risk / Opportunity / Action / Confidence / Alternative format.
- **Deadline questions** (`/deadline`, or any message containing "last date", "deadline", "close admissions" ...)
  produce a Deadline Decision Brief: web-verified calendar check, what history says (from Last Dates Performance
  and the day-wise tabs), a scored candidate-date table, one-line "why not" for each rejected date, the
  recommended date with announcement and single-extension dates, expected deadline-day registrations, campus /
  programme exceptions, risks, confidence, alternative, next action.
- Replies on Telegram end with `⏱ seconds · model · tools used`.

One-time setup:
1. Google Cloud Console -> create (or pick) a project -> **APIs & Services -> Enable** the *Google Sheets API*.
2. **IAM & Admin -> Service Accounts -> Create** (any name, no roles needed) -> **Keys -> Add key -> JSON**.
   Save the downloaded file as `data/google-service-account.json` (git-ignored).
3. Open the sheet -> **Share** -> add the service account's email (`...@...iam.gserviceaccount.com`) as **Viewer**.
4. Restart the bot (or send `/reload`). The log shows `reference sheet loaded: ... rows`.

`REFERENCE_SHEET_ID` / `REFERENCE_SHEET_TABS` in `.env` change which sheet and tabs are read; the cache refreshes
every `REFERENCE_REFRESH_HOURS` (default 6).

## 6. What a turn looks like

Telegram: a "🔎 Working on it…" message that updates live with each portal tool call (✓ / ✕ and seconds), then
the answer, split into ≤4 096-character messages with headings in bold and tables in monospace.
WhatsApp cannot edit messages, so it sends one "working on it" line, then the answer.
A `/daily` run typically makes 5–10 portal calls and takes one to three minutes.

## 7. Production notes

- Run it 24/7 as a service: Task Scheduler (run `run.bat` at startup) or NSSM on Windows, systemd on Linux on a machine that can reach both the
  Claude API and the PUAP MCP server. Conversations are in memory and reset on restart.
- Keep `data/users.json` and `.env` out of version control (already in `.gitignore`).
- The PUAP MCP server should enforce scope server-side as well (defence in depth). Portal RBAC hygiene matters
  more than this bot's gate: 77 accounts hold System Administrator and 533 hold "Manage All" in the portal, and
  test / dummy roles are active.
- `ANTHROPIC_FALLBACKS=1` uses the Claude API's server-side refusal fallback; set `0` on Bedrock / Vertex.

## 8. Which model powers the brain

`LLM_PROVIDER` in `.env`: `gemini` (Google Developer API **free tier** - no billing; auto-picks the newest Flash
model, health-probes it at startup, budgets itself to 14 requests/min, waits on quota hits and fails over to other
Flash / Flash-Lite models when Google is overloaded), `mistral` (free Experiment plan), `openai_compat` (any
OpenAI-compatible endpoint - NVIDIA build.nvidia.com free key with `nvidia/nemotron-3-super-120b-a12b` is set up),
`anthropic` (Claude Opus 5 via prepaid credits - best quality) or `vertex` (Claude billed to a Google Cloud project).
`LLM_FALLBACK_PROVIDERS` (default `mistral,openai_compat,anthropic`) are tried in order for a question when the
primary is rate-limited or overloaded; unconfigured ones are skipped. Measured: Gemini lookups 20-45 s, briefs
60-90 s when healthy; NVIDIA backup ~5 min per brief. Free-tier note: Google may use unpaid-tier prompts to improve its products - keep
identifiable student data out of the bot on that tier.

## 9. Moving the bot to another machine (GitHub is the source of truth)

Everything except secrets is in the repository. Three files are deliberately git-ignored and must be copied by hand:
`.env` (API keys and tokens), `data/users.json` (who is enrolled), `data/google-service-account.json` (sheet access).
Clone, run `setup.bat`, drop those three files into place, run `run.bat`. Deleting the folder on the old machine
stops the bot there - only one copy should run at a time (two copies fight over Telegram updates).
