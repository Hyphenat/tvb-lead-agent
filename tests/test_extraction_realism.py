"""Extraction tested against the shapes real pages actually use.

Every pattern here was written after checking the extractor against realistic
text and finding it wrong. Synthetic fixtures flatter a regex; team pages,
imprints and press copy do not. These cases exist so the next change has to keep
clearing the same bar.
"""

import asyncio

import pytest

from tvb_agent.models import SourceAuthority
from tvb_agent.research.extractor import GroundedExtractor, Source

HARD_US_SIGNALS = {"us_office", "us_subsidiary", "us_incorporation", "us_job_posting"}


def extract(text: str, company: str = "Acme", url: str = "https://acme.io/team"):
    src = Source(url=url, text=text, authority=SourceAuthority.COMPANY_OWNED)
    return asyncio.run(GroundedExtractor(None).extract(company, [src]))


def founders(text: str) -> list[str]:
    return [str(c.value) for c in extract(text).by_field("founder")]


def us_signals(text: str) -> set[str]:
    return {str(c.value) for c in extract(text, url="https://acme.io/about").by_field("us_signal")}


# --------------------------------------------------------------------------- #
# Founder identification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    # The commonest team-page layout by far: name and role on separate lines.
    ("Our Leadership\nPriya Raman\nCo-Founder & CEO\nPriya leads product.\nArjun Mehta\nCTO",
     "Priya Raman"),
    # Prose.
    ("Meet the team behind Acme. Ravi Kumar is our Founder and Chief Executive Officer.",
     "Ravi Kumar"),
    # Em dash, which typographic themes use in place of a comma.
    ("Leadership\n\nMaria Silva Santos - Co-founder and CEO\nJoao Pereira - Head of Engineering",
     "Maria Silva Santos"),
    # The role is stated after the name, further along the sentence.
    ("The company was founded in 2021 by Lena Brandt, who serves as Chief Executive Officer.",
     "Lena Brandt"),
    # Pipe-separated, as used in card layouts and social bios.
    ("Chidi Okonkwo | Founder & CEO | Nova Pay", "Chidi Okonkwo"),
    # Title first, as press copy writes it.
    ("Speaking to press, CEO Ahmed Al-Mansouri said the round would fund expansion.",
     "Ahmed Al-Mansouri"),
])
def test_founder_is_found_in_real_page_layouts(text, expected):
    assert expected in founders(text)


@pytest.mark.parametrize("text", [
    "Our team of 40 engineers works from Berlin. Contact us for more.",
    "Jane Doe, VP of Marketing, joined last year. Bob Smith is Head of Sales.",
    "We are a team of designers, engineers and operators.",
])
def test_non_founders_and_unnamed_teams_yield_nothing(text):
    assert founders(text) == []


def test_a_founder_claim_always_carries_its_quote():
    result = extract("Priya Raman\nCo-Founder & CEO\n")
    for claim in result.by_field("founder"):
        assert claim.evidence.quote
        assert claim.evidence.url == "https://acme.io/team"


# --------------------------------------------------------------------------- #
# US presence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    # Written far more often this way round than "New York office".
    "We have offices in London and New York.",
    "Our presence spans London, Paris and San Francisco.",
    "Our US subsidiary, Acme Inc., serves North American customers.",
    "Acme Technologies Inc. is incorporated in Delaware.",
    "Open roles: Sales Director (San Francisco, CA), Engineer (Bengaluru)",
])
def test_genuine_us_footprint_is_detected(text):
    assert us_signals(text) & HARD_US_SIGNALS, f"missed US presence in: {text!r}"


@pytest.mark.parametrize("text", [
    "Helios Grid GmbH is headquartered in Munich with a team across Germany.",
    "We have offices in Berlin and Warsaw.",
    "Open roles: Backend Engineer (Bengaluru), Designer (Pune)",
    # Selling to US customers is not the same as having a US presence.
    "We serve customers in over 30 countries including the United States.",
])
def test_non_us_companies_are_not_falsely_flagged(text):
    assert not (us_signals(text) & HARD_US_SIGNALS), f"false US signal in: {text!r}"


def test_us_phone_number_is_only_a_weak_signal():
    signals = us_signals("Call us on +1 (415) 555-0132")
    assert "us_phone" in signals
    assert not (signals & HARD_US_SIGNALS), "a phone number alone must not disqualify a company"


# --------------------------------------------------------------------------- #
# Imprint pages
# --------------------------------------------------------------------------- #
# Imprint-law markets are weighted heavily in discovery precisely because the
# law requires a named, contactable representative - and the agent was reading
# the address off the imprint while ignoring the name printed above it.
@pytest.mark.parametrize("text,person,accepted", [
    ("Impressum\nVertreten durch: Dr. Lena Brandt (Geschäftsführerin)", "Lena Brandt", True),
    ("Impressum\nGeschäftsführer: Markus Weber", "Markus Weber", True),
    ("Mentions légales\nGérant : Camille Dupont", "Camille Dupont", True),
    ("Colofon\nBestuurder: Jeroen van Dijk", "Jeroen van Dijk", True),
    ("Prezes Zarządu: Kasia Nowak", "Kasia Nowak", True),
    ("Amministratore delegato: Marco Rossi", "Marco Rossi", True),
    # These offices are *not* the chief executive, and must not be offered to
    # TVB as a founder contact.
    ("Mentions légales\nDirecteur de la publication : Camille Dupont", "Camille Dupont", False),
    ("Legale rappresentante: Marco Rossi", "Marco Rossi", False),
    ("Vorstand: Klaus Meier", "Klaus Meier", False),
])
def test_imprint_offices_are_read_and_only_chief_executives_are_accepted(text, person, accepted):
    from tvb_agent.validation.validators import validate_founder

    result = extract(text, url="https://acme.de/impressum")
    assert person in [str(c.value) for c in result.by_field("founder")]

    founder = validate_founder(result)
    assert bool(founder.known) is accepted
    if accepted:
        assert founder.value.name == person


@pytest.mark.parametrize("text,expected", [
    ("Colofon\nBestuurder: Jeroen van Dijk", "Jeroen van Dijk"),
    ("Aviso legal\nAdministrador: Ana de Souza", "Ana de Souza"),
    ("Fundada por Ana Paula dos Santos, CEO.", "Ana Paula dos Santos"),
])
def test_surnames_with_lowercase_particles_survive(text, expected):
    """A pattern insisting on initial capitals loses the person entirely."""
    assert expected in founders(text)


@pytest.mark.parametrize("text", [
    "Our team of 40 engineers works from Berlin.",
    "Jane Doe, VP of Marketing, joined last year.",
])
def test_particles_do_not_open_the_door_to_sentence_fragments(text):
    assert founders(text) == []


@pytest.mark.parametrize("label", [
    "Personal E-Mail",     # run 14 shipped this as a founder's name
    "Registered Office", "Contact Details", "Email Address",
    "Press Office", "General Enquiries", "Postal Address",
])
def test_a_form_label_is_never_read_as_a_person(label):
    """These captions sit directly beside the fields the extractor reads, so
    they are the likeliest words in the whole pipeline to be mistaken for a
    name - and one of them reached a delivered lead."""
    from tvb_agent.validation.validators import names_a_role_not_a_person

    assert names_a_role_not_a_person(label)


@pytest.mark.parametrize("name", [
    "Fabian Fussek", "Dennis Green-Lieber", "Francesco Baschieri",
    "Priya Raman", "Ana de Souza",
])
def test_real_founders_survive_the_label_filter(name):
    from tvb_agent.validation.validators import names_a_role_not_a_person

    assert not names_a_role_not_a_person(name)


def test_a_german_imprint_label_does_not_become_the_founder():
    from tvb_agent.validation.validators import validate_founder

    text = ("Impressum\nKaiko Systems GmbH\n"
            "Personal E-Mail: f.fussek@kaikosystems.com\n"
            "Registered Office: Berlin\n")
    assert not validate_founder(extract(text, url="https://kaikosystems.com/imprint")).known
