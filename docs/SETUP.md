# Running this yourself, with your own API keys

Three ways to run the agent, depending on what you want to do. All of them work
with free-tier keys, and none of them needs a key from the author.

---

## 1. The hosted app — paste your key into the sidebar

The quickest way to see it work on your own credits. Nothing to install.

1. Open the live app.
2. In the sidebar, open **🔑 Use your own API keys**.
3. Paste a Serper key into the first box. That is the only one that really
   matters — without a search key the agent has nothing to discover with.
4. Press **Run the agent now**.

**Your key lives in your browser session only.** It is never written to disk,
never logged, and disappears when you close the tab. Other people using the same
URL are unaffected by it, and you are unaffected by theirs. There is a **Clear my
keys** button if you want to remove it before you finish.

This exists because the deployment ships with the author's free-tier keys, and
free allowances run out. A reviewer arriving after they are spent would
otherwise see an agent that cannot search — which says nothing about whether it
works.

---

## 2. On your own machine

```bash
git clone https://github.com/<user>/tvb-lead-agent.git
cd tvb-lead-agent

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements-dev.txt
python -m pytest -q            # 565 tests, no API key needed
```

The test suite runs the entire pipeline against a simulated web with no network
access, so you can verify the logic before spending a single credit.

Then add your keys:

```bash
cp .env.example .env           # Windows: copy .env.example .env
```

> **Careful:** run that copy **once**. Running it again overwrites `.env` and
> silently wipes the keys you already put there.

Open `.env` in any text editor and fill in the keys you have:

```ini
SERPER_API_KEY=your-key-here        # the only one that is really required
HUNTER_API_KEY=your-key-here        # finds and verifies founder emails
GEMINI_API_KEY=your-key-here        # optional; improves descriptions
ZEROBOUNCE_API_KEY=your-key-here    # optional; email deliverability
APOLLO_API_KEY=your-key-here        # optional; free tier will not release emails
```

Check they work before running anything long:

```bash
python tvb.py doctor --live
```

This calls each provider once and tells you, in plain words, which keys are
accepted, how many credits remain, and which ones are set but broken. **A key
that is set but invalid is worse than a blank one** — the agent reports the
provider as available and then every call fails quietly behind the fallback
chain, so the doctor names those explicitly.

Then:

```bash
python tvb.py run                       # discover and qualify
python tvb.py leads                     # every verified lead on file
python tvb.py leads --csv leads.csv --json leads.json
python tvb.py audit leads.json          # re-verify an export from scratch
streamlit run app/streamlit_app.py      # the web interface, locally
```

> Use `python tvb.py ...`, not `python -m tvb_agent.cli ...`. The package lives
> under `src/`, so the `-m` form only resolves once the project is installed.
> `tvb.py` puts `src` on the path and calls the same CLI.

---

## 3. Deploying your own copy

On **Streamlit Community Cloud**: point it at your fork, main file
`app/streamlit_app.py`, then **Manage app → Settings → Secrets** and paste:

```toml
SERPER_API_KEY = "your-key"
HUNTER_API_KEY = "your-key"
GEMINI_API_KEY = "your-key"
ZEROBOUNCE_API_KEY = "your-key"

TARGET_QUALIFIED = "15"
MAX_RUNTIME_SECONDS = "900"
```

The agent reads `st.secrets` as well as environment variables, so no code change
is needed. `.streamlit/secrets.toml.example` is the template.

---

## Where to get each key

| Key | Where | Free tier | What breaks without it |
|---|---|---|---|
| `SERPER_API_KEY` | serper.dev | 2,500 searches, **for the life of the account** | Everything. The agent cannot discover companies. |
| `HUNTER_API_KEY` | hunter.io | a few dozen finder lookups + 50 verifications a month | Founder emails are only found when published on a readable page |
| `GEMINI_API_KEY` | aistudio.google.com | rate-limited, resets daily | Descriptions get thinner; every gate still works on deterministic rules |
| `ZEROBOUNCE_API_KEY` | zerobounce.net | 100 verifications | Deliverability falls back to authoritative-source attribution + MX, recorded on each lead |
| `APOLLO_API_KEY` | apollo.io | **will not release email addresses via API** | Nothing — it is tried second and its free tier returns a placeholder |

## Budget settings

Any of these can go in `.env` or in Streamlit secrets. **A value set there
overrides the default in `config.py`** — worth knowing, because a stale
`MAX_SEARCHES` in `.env` will silently cap a run you thought you had widened.

```ini
TARGET_QUALIFIED=15
MAX_SEARCHES=700
MAX_COMPANIES=250
MAX_PAGES=900
MAX_RUNTIME_SECONDS=2700
```

## Security

- `.env`, `.streamlit/secrets.toml`, the database and all CSV/JSON exports are
  in `.gitignore`. No key can reach the repository by accident.
- No key is ever written to a log line, a lead, or an export.
- Keys pasted into the hosted app are session-scoped and held in memory only.
