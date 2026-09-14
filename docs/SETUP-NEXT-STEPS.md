# Next steps

## 1. Install and verify (no API keys needed)

```powershell
cd C:\Users\fsarg\OneDrive\Desktop\TVB

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements-dev.txt
python -m pytest -q
```

Expect **239 passed**. That runs the whole pipeline against a simulated web with
no network access, so the logic is verified before any key exists.

## 2. Add your keys

```powershell
copy .env.example .env
notepad .env
python tvb.py doctor
```

`doctor` should show three configured providers.

> **Use `python tvb.py ...`, not `python -m tvb_agent.cli ...`.**
> The package lives under `src/`, so the `-m` form only resolves once the project
> is installed. `tvb.py` puts `src` on the path and calls the same CLI.
> If you would rather install it properly, `pip install -e .` also works and then
> both forms (and a `tvb-agent` command) work.

Keys come from:

| Key | Where | Free tier |
|---|---|---|
| Serper | https://serper.dev | 2,500 searches |
| Gemini | https://aistudio.google.com/apikey | generous |
| ZeroBounce | https://zerobounce.net | 100 verifications |

A Gemini key from AI Studio starts with `AIza`. If yours starts with anything
else, it is a different kind of credential and will fail authentication.

## 3. First real run

```powershell
python tvb.py run --target 15 --json leads.json --csv leads.csv --stats stats.json -v
```

This writes `leads.json`, `leads.csv` and `stats.json` into this folder.
**Tell Claude when it finishes** - it can read those files directly from here and
tune the pipeline against what actually came back.

Then re-verify the output independently:

```powershell
python tvb.py audit leads.json --json audit-report.json
```

Or use the interface:

```powershell
streamlit run app/streamlit_app.py
```

## 4. GitHub workflow file

`docs/ci-workflow.yml` could not be written to `.github/workflows/` directly
(that path is protected from remote writes). Move it yourself:

```powershell
mkdir .github\workflows
move docs\ci-workflow.yml .github\workflows\ci.yml
```

## 5. Publishing (after the run looks good)

```powershell
git init
git add .
git commit -m "TVB lead discovery and qualification agent"
git branch -M main
git remote add origin https://github.com/<you>/tvb-lead-agent.git
git push -u origin main
```

Then deploy at https://share.streamlit.io - main file path `app/streamlit_app.py`,
and paste the keys into **Advanced settings -> Secrets** in TOML form
(see `.streamlit/secrets.toml.example`).

`.gitignore` already excludes `.env`, the database, and any CSV/JSON output, so
no secrets or run data can be committed by accident. Before pushing, confirm:

```powershell
git status --short
```

`.env` must not appear in that list.

## Housekeeping

- Delete the stray `.venv.venv` folder if it is still there.
- `pytest.ini` was folded into `pyproject.toml`; you can delete it.
