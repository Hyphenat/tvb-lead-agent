"""The bank of leads that ships with the repository.

A reviewer opens the hosted link before they decide whether to spend their own
API credits, so the page has to open with the agent's real output. That is only
honest if the shipped leads are held to exactly the rules a freshly-found lead
is held to - which is what these tests pin down.
"""

import asyncio
import json

import pytest
import respx

from tvb_agent.agent import LeadAgent
from tvb_agent.seed import export_bank, load_bank, shipped_company_ids
from tvb_agent.storage import Store

from .test_integration import configure, install_fake_web


@pytest.fixture
def banked(settings, store):
    @respx.mock
    async def go():
        install_fake_web()
        return await LeadAgent(settings=settings, store=store).run(seed=7)

    configure(settings)
    settings.budget.target_qualified = 2
    asyncio.run(go())
    leads = store.leads(qualified_only=True)
    assert leads, "the fake web must yield at least one lead for this test to mean anything"
    return leads


def test_bank_round_trips_into_an_empty_database(banked, store, tmp_path):
    path = tmp_path / "shipped_leads.json"
    assert export_bank(store, path) == len(banked)

    fresh = Store(tmp_path / "fresh.sqlite3")
    # Rejections travel alongside the leads, so more records load than qualify.
    assert load_bank(fresh, path) >= len(banked)
    replayed = fresh.leads(qualified_only=True)
    assert [lead.company.name for lead in replayed] == [lead.company.name for lead in banked]

    # The evidence travels; a shipped lead a reviewer opens can still be checked.
    first = replayed[0].company
    assert first.all_evidence(), "evidence must survive the round trip"
    assert first.email.address == banked[0].company.email.address
    assert replayed[0].qualification.gates, "gate reasons must survive the round trip"
    fresh.close()


def test_replaying_twice_does_not_duplicate(banked, store, tmp_path):
    path = tmp_path / "shipped_leads.json"
    export_bank(store, path)
    fresh = Store(tmp_path / "fresh.sqlite3")
    load_bank(fresh, path)
    load_bank(fresh, path)
    assert len(fresh.leads(qualified_only=True)) == len(banked)
    fresh.close()


def test_the_run_that_found_each_lead_travels_with_it(banked, store, tmp_path):
    path = tmp_path / "shipped_leads.json"
    export_bank(store, path)
    fresh = Store(tmp_path / "fresh.sqlite3")
    load_bank(fresh, path)
    ids = {r["id"] for r in fresh.list_runs(50)}
    assert {lead.run_id for lead in banked} <= ids, "run history must say which run found what"
    fresh.close()


def test_a_shipped_lead_still_faces_the_current_rules(banked, store, tmp_path):
    """The whole point: shipping a lead is not a way around a gate.

    A lead is doctored in the file to one the current rules reject - a role
    account for an email - and must not come back out of the reader.
    """
    path = tmp_path / "shipped_leads.json"
    export_bank(store, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    victim = payload["leads"][0]
    domain = victim["company"]["email"]["address"].split("@")[-1]
    victim["company"]["email"]["address"] = f"info@{domain}"
    path.write_text(json.dumps(payload), encoding="utf-8")

    fresh = Store(tmp_path / "fresh.sqlite3")
    assert load_bank(fresh, path) >= len(banked)      # it loads
    names = [lead.company.name for lead in fresh.leads(qualified_only=True)]
    assert victim["company"]["name"] not in names, (
        "a shipped lead that fails the current rules must be dropped on read, "
        "exactly like a locally-found one"
    )
    fresh.close()


def test_a_missing_or_unreadable_bank_is_not_an_error(tmp_path):
    fresh = Store(tmp_path / "fresh.sqlite3")
    assert load_bank(fresh, tmp_path / "nope.json") == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_bank(fresh, bad) == 0
    assert shipped_company_ids(tmp_path / "nope.json") == set()
    fresh.close()


def test_shipped_ids_name_the_companies_in_the_file(banked, store, tmp_path):
    path = tmp_path / "shipped_leads.json"
    export_bank(store, path)
    assert shipped_company_ids(path) == {lead.company.id for lead in banked}


def test_rejections_travel_and_stay_rejections(banked, store, tmp_path):
    """The rejected companies are the honest answer to 'why so few?'.

    They ship so a reviewer can read the funnel, and they must come back as
    rejections - never quietly promoted into the lead list.
    """
    path = tmp_path / "shipped_leads.json"
    export_bank(store, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert any(not rec["qualified"] for rec in payload["leads"]), \
        "a real run rejects companies; those rejections are part of the record"

    fresh = Store(tmp_path / "fresh.sqlite3")
    load_bank(fresh, path)
    qualified = fresh.leads(qualified_only=True)
    everything = fresh.leads(qualified_only=False)
    assert len(everything) > len(qualified)
    assert all(lead.qualification.qualified for lead in qualified)
    for lead in everything:
        if not lead.qualification.qualified:
            assert any(not g.passed for g in lead.qualification.gates), \
                "a rejection must say which requirement it failed"
    fresh.close()
