"""SQLite persistence.

Three things depend on this store, all of them required by the brief:

* **Deduplication across runs** - a company seen in run 1 is recognised in run 7.
* **Genuine novelty on re-runs** - the frontier table records which
  (sector x geography x phrasing x window x source-shape) cells have been
  explored and what they yielded, so the planner can prefer unexplored ground.
* **Partial-result durability** - everything is written as it is discovered, so a
  run that is cut short by a hosting timeout still leaves usable leads behind.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import CompanyProfile, Lead, Qualification

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    params_json TEXT,
    stats_json TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    domain TEXT,
    profile_json TEXT NOT NULL,
    first_seen_run TEXT,
    last_seen_run TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
CREATE INDEX IF NOT EXISTS idx_companies_namenorm ON companies(name_norm);

CREATE TABLE IF NOT EXISTS aliases (
    alias_norm TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS qualifications (
    company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    qualified INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    fit_score REAL NOT NULL DEFAULT 0,
    near_boundary INTEGER NOT NULL DEFAULT 0,
    gates_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (company_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_qual_run ON qualifications(run_id, qualified);

CREATE TABLE IF NOT EXISTS frontier (
    cell_key TEXT PRIMARY KEY,
    sector TEXT, geography TEXT, phrasing TEXT, window TEXT, source_shape TEXT,
    times_used INTEGER NOT NULL DEFAULT 0,
    candidates_found INTEGER NOT NULL DEFAULT 0,
    qualified_found INTEGER NOT NULL DEFAULT 0,
    last_used_run TEXT,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    query TEXT NOT NULL,
    provider TEXT,
    cell_key TEXT,
    results_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queries_text ON queries(query);

CREATE TABLE IF NOT EXISTS email_checks (
    address TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_cache (
    url TEXT PRIMARY KEY,
    status INTEGER,
    text TEXT,
    title TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    stage TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runlog_run ON run_log(run_id, id);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalise_name(name: str) -> str:
    """Fold a company name to a comparison key.

    Only *legal entity* suffixes are stripped, so "Acme Technologies Pvt. Ltd."
    and "Acme Technologies" collapse together.  Descriptive words such as
    "Labs", "Software" or "Technologies" are deliberately kept: stripping them
    would merge "Acme Labs" and "Acme Software" into one company, which is a
    far worse error than failing to merge two spellings of the same one.
    """
    n = (name or "").lower().strip()
    n = re.sub(r"[\u2019']", "", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    legal_suffixes = {
        "inc", "llc", "lllp", "llp", "ltd", "limited", "pvt", "private", "plc", "gmbh", "ug",
        "bv", "nv", "sa", "sas", "sarl", "ab", "as", "oy", "oyj", "aps", "srl", "spa", "sl",
        "pte", "sdn", "bhd", "kk", "ao", "pjsc", "fzc", "fze", "dmcc", "co", "corp",
        "corporation", "company", "incorporated", "holdings", "holding", "the",
    }
    toks = [t for t in n.split() if t]
    while toks and toks[-1] in legal_suffixes:
        toks.pop()
    while toks and toks[0] in {"the"}:
        toks.pop(0)
    return " ".join(toks) or n.strip()


def normalise_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0]
    d = d[4:] if d.startswith("www.") else d
    return d or None


class Store:
    """Thread-safe-enough SQLite wrapper (one connection guarded by a lock)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def _cur(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- runs --
    def start_run(self, run_id: str, params: dict[str, Any]) -> None:
        with self._cur() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs (id, started_at, status, params_json) VALUES (?,?,?,?)",
                (run_id, now_iso(), "running", json.dumps(params, default=str)),
            )

    def finish_run(self, run_id: str, status: str, stats: dict[str, Any], note: str = "") -> None:
        with self._cur() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, stats_json=?, note=? WHERE id=?",
                (now_iso(), status, json.dumps(stats, default=str), note, run_id),
            )

    def record_run_row(self, row: dict[str, Any]) -> None:
        """Insert a run exactly as it was recorded elsewhere, timestamps intact.

        Used when a bank of leads is replayed from ``data/shipped_leads.json``:
        the run that found a lead should keep its own id, start time and stats,
        so the history tab says what that run actually cost rather than when the
        file happened to be imported.
        """
        with self._cur() as c:
            c.execute(
                """INSERT OR REPLACE INTO runs
                   (id, started_at, finished_at, status, params_json, stats_json, note)
                   VALUES (?,?,?,?,?,?,?)""",
                (str(row.get("id")), row.get("started_at") or now_iso(),
                 row.get("finished_at"), row.get("status") or "finished",
                 row.get("params_json"), row.get("stats_json"), row.get("note") or ""),
            )

    def list_runs(self, limit: int = 25) -> list[dict]:
        with self._cur() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def log(self, run_id: str, message: str, level: str = "info", stage: str = "") -> None:
        with self._cur() as c:
            c.execute(
                "INSERT INTO run_log (run_id, ts, level, stage, message) VALUES (?,?,?,?,?)",
                (run_id, now_iso(), level, stage, message),
            )

    def get_log(self, run_id: str, after_id: int = 0, limit: int = 500) -> list[dict]:
        with self._cur() as c:
            rows = c.execute(
                "SELECT * FROM run_log WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                (run_id, after_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- companies --
    def find_company(self, name: str, domain: str | None) -> dict | None:
        """Existing record matched by domain first, then by normalised name/alias."""
        dom, nn = normalise_domain(domain), normalise_name(name)
        with self._cur() as c:
            if dom:
                r = c.execute("SELECT * FROM companies WHERE domain=?", (dom,)).fetchone()
                if r:
                    return dict(r)
            r = c.execute("SELECT * FROM companies WHERE name_norm=?", (nn,)).fetchone()
            if r:
                return dict(r)
            r = c.execute(
                "SELECT c.* FROM aliases a JOIN companies c ON c.id=a.company_id WHERE a.alias_norm=?",
                (nn,),
            ).fetchone()
            return dict(r) if r else None

    def upsert_company(self, profile: CompanyProfile, run_id: str) -> None:
        with self._cur() as c:
            existing = c.execute("SELECT first_seen_run FROM companies WHERE id=?", (profile.id,)).fetchone()
            first = existing["first_seen_run"] if existing and existing["first_seen_run"] else run_id
            c.execute(
                """INSERT INTO companies (id,name,name_norm,domain,profile_json,first_seen_run,last_seen_run,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, name_norm=excluded.name_norm, domain=excluded.domain,
                     profile_json=excluded.profile_json, last_seen_run=excluded.last_seen_run,
                     updated_at=excluded.updated_at""",
                (profile.id, profile.name, normalise_name(profile.name), normalise_domain(profile.domain),
                 profile.model_dump_json(), first, run_id, now_iso()),
            )
            c.execute("INSERT OR IGNORE INTO aliases (alias_norm, company_id) VALUES (?,?)",
                      (normalise_name(profile.name), profile.id))

    def get_company(self, company_id: str) -> CompanyProfile | None:
        with self._cur() as c:
            r = c.execute("SELECT profile_json FROM companies WHERE id=?", (company_id,)).fetchone()
        return CompanyProfile.model_validate_json(r["profile_json"]) if r else None

    def known_company_keys(self) -> set[str]:
        """Domains and normalised names already seen - the cheap dedup filter."""
        with self._cur() as c:
            rows = c.execute("SELECT domain, name_norm FROM companies").fetchall()
            al = c.execute("SELECT alias_norm FROM aliases").fetchall()
        keys = {r["domain"] for r in rows if r["domain"]} | {r["name_norm"] for r in rows if r["name_norm"]}
        return keys | {r["alias_norm"] for r in al if r["alias_norm"]}

    def company_count(self) -> int:
        with self._cur() as c:
            return int(c.execute("SELECT COUNT(*) n FROM companies").fetchone()["n"])

    # ------------------------------------------------------ qualifications --
    def save_qualification(self, company_id: str, run_id: str, q: Qualification) -> None:
        with self._cur() as c:
            c.execute(
                """INSERT OR REPLACE INTO qualifications
                   (company_id,run_id,qualified,confidence,fit_score,near_boundary,gates_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (company_id, run_id, int(q.qualified), q.confidence, q.fit_score,
                 int(q.near_boundary), q.model_dump_json(), now_iso()),
            )

    def leads(self, run_id: str | None = None, qualified_only: bool = True) -> list[Lead]:
        sql = """SELECT q.*, c.profile_json FROM qualifications q
                 JOIN companies c ON c.id=q.company_id WHERE 1=1"""
        args: list[Any] = []
        if run_id:
            sql += " AND q.run_id=?"
            args.append(run_id)
        if qualified_only:
            sql += " AND q.qualified=1"
        sql += " ORDER BY q.qualified DESC, q.fit_score DESC, q.confidence DESC"
        with self._cur() as c:
            rows = c.execute(sql, args).fetchall()
        from .validation.validators import lead_fails_current_rules

        out: list[Lead] = []
        self.last_read_rejections: list[tuple[str, str]] = []
        for r in rows:
            profile = CompanyProfile.model_validate_json(r["profile_json"])
            # Leads banked under older, weaker rules are not grandfathered in:
            # a fix applies retroactively, so a hole that has been closed cannot
            # keep exporting the lead it let through.
            if qualified_only:
                why = lead_fails_current_rules(profile)
                if why:
                    self.last_read_rejections.append((profile.name, why))
                    continue
            out.append(
                Lead(
                    company=profile,
                    qualification=Qualification.model_validate_json(r["gates_json"]),
                    run_id=r["run_id"],
                )
            )
        return out

    def all_time_qualified_count(self) -> int:
        with self._cur() as c:
            return int(c.execute(
                "SELECT COUNT(DISTINCT company_id) n FROM qualifications WHERE qualified=1"
            ).fetchone()["n"])

    # ------------------------------------------------------------ frontier --
    def bump_cell(self, cell_key: str, parts: dict[str, str], run_id: str,
                  candidates: int = 0, qualified: int = 0) -> None:
        with self._cur() as c:
            c.execute(
                """INSERT INTO frontier (cell_key,sector,geography,phrasing,window,source_shape,
                                         times_used,candidates_found,qualified_found,last_used_run,last_used_at)
                   VALUES (?,?,?,?,?,?,1,?,?,?,?)
                   ON CONFLICT(cell_key) DO UPDATE SET
                     times_used=times_used+1,
                     candidates_found=candidates_found+excluded.candidates_found,
                     qualified_found=qualified_found+excluded.qualified_found,
                     last_used_run=excluded.last_used_run,
                     last_used_at=excluded.last_used_at""",
                (cell_key, parts.get("sector"), parts.get("geography"), parts.get("phrasing"),
                 parts.get("window"), parts.get("source_shape"), candidates, qualified, run_id, now_iso()),
            )

    def cell_stats(self) -> dict[str, dict]:
        with self._cur() as c:
            rows = c.execute("SELECT * FROM frontier").fetchall()
        return {r["cell_key"]: dict(r) for r in rows}

    def seen_queries(self) -> set[str]:
        with self._cur() as c:
            return {r["query"] for r in c.execute("SELECT DISTINCT query FROM queries").fetchall()}

    def record_query(self, run_id: str, query: str, provider: str, cell_key: str, n: int) -> None:
        with self._cur() as c:
            c.execute(
                "INSERT INTO queries (run_id,query,provider,cell_key,results_count,created_at) VALUES (?,?,?,?,?,?)",
                (run_id, query, provider, cell_key, n, now_iso()),
            )

    # -------------------------------------------------------- email cache --
    def get_email_check(self, address: str, max_age_seconds: int = 30 * 86400) -> dict | None:
        with self._cur() as c:
            r = c.execute("SELECT * FROM email_checks WHERE address=?", (address.lower(),)).fetchone()
        if not r:
            return None
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(r["checked_at"])).total_seconds()
            if age > max_age_seconds:
                return None
        except Exception:
            return None
        return json.loads(r["record_json"])

    def save_email_check(self, address: str, record: dict) -> None:
        with self._cur() as c:
            c.execute(
                "INSERT OR REPLACE INTO email_checks (address,record_json,checked_at) VALUES (?,?,?)",
                (address.lower(), json.dumps(record, default=str), now_iso()),
            )

    # --------------------------------------------------------- page cache --
    def get_page(self, url: str, ttl: int) -> dict | None:
        with self._cur() as c:
            r = c.execute("SELECT * FROM page_cache WHERE url=?", (url,)).fetchone()
        if not r:
            return None
        try:
            if (datetime.now(UTC) - datetime.fromisoformat(r["fetched_at"])).total_seconds() > ttl:
                return None
        except Exception:
            return None
        return dict(r)

    def save_page(self, url: str, status: int, text: str, title: str = "") -> None:
        with self._cur() as c:
            c.execute(
                "INSERT OR REPLACE INTO page_cache (url,status,text,title,fetched_at) VALUES (?,?,?,?,?)",
                (url, status, text[:400_000], title, now_iso()),
            )
