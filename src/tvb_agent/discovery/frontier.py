"""In-run candidate queue.

Keeps the candidates found so far, collapses duplicates as they arrive, and
hands them out in priority order.  Candidates corroborated by several
independent discovery sources are investigated first, because a company that
shows up in a funding article *and* on a VC portfolio page is a better use of a
research slot than one seen once.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..storage import normalise_domain
from .candidate_extractor import Candidate


@dataclass
class Frontier:
    known_keys: set[str] = field(default_factory=set)   # seen in previous runs
    _queue: dict[str, Candidate] = field(default_factory=dict)
    _index: dict[str, Candidate] = field(default_factory=dict)
    _done: set[str] = field(default_factory=set)
    skipped_known: int = 0
    merged: int = 0
    # TVB's band, so a candidate whose headline already puts it outside can be
    # dropped before it ever becomes research.
    min_usd: float = 1_000_000.0
    max_usd: float = 5_000_000.0
    ruled_out: int = 0
    ruled_out_reasons: dict = field(default_factory=dict)

    def _keys_for(self, candidate: Candidate) -> list[str]:  # noqa: D401
        """A candidate is identified by its domain *and* its normalised name.

        Discovery finds the same company both ways - "Zeta Care" from a headline
        and ``zetacare.in`` from a portfolio link - and they must collapse into
        one entry, otherwise the same company is researched twice and the one
        without a domain is researched badly.
        """
        from ..storage import normalise_domain, normalise_name

        keys: list[str] = []
        dom = normalise_domain(candidate.domain)
        if dom:
            keys.append(dom)
        nm = normalise_name(candidate.name)
        if nm:
            keys.append(nm)
        return keys

    def _merge(self, existing: Candidate, incoming: Candidate) -> None:
        for src in incoming.discovered_via:
            if src not in existing.discovered_via:
                existing.discovered_via.append(src)
        if not existing.domain and incoming.domain:
            existing.domain = incoming.domain
        if len(incoming.hint_snippet) > len(existing.hint_snippet):
            existing.hint_snippet = incoming.hint_snippet
        # Keep the strongest provenance the company was seen with.
        if self._KIND_RANK.get(incoming.source_kind, 4) < self._KIND_RANK.get(existing.source_kind, 4):
            existing.source_kind = incoming.source_kind

    def add(self, candidate: Candidate, *, skip_known: bool = True) -> bool:
        """Returns True when this is a genuinely new candidate for this run."""
        keys = self._keys_for(candidate)
        if not keys:
            return False
        if any(k in self._done for k in keys):
            return False
        if skip_known and any(k in self.known_keys for k in keys):
            self.skipped_known += 1
            return False

        for k in keys:
            if k in self._index:
                existing = self._index[k]
                # Two candidates that each name a *different* website are two
                # different companies, however similar their names look.
                if (existing.domain and candidate.domain
                        and normalise_domain(existing.domain) != normalise_domain(candidate.domain)):
                    continue
                self._merge(existing, candidate)
                for nk in self._keys_for(existing):
                    self._index[nk] = existing
                self.merged += 1
                return False

        # If the excerpt that found this company already rules it out - a round
        # far outside the band, a US headline - it never becomes research. This
        # costs nothing and is the cheapest filter in the pipeline.
        from .candidate_extractor import assess_candidate

        verdict = assess_candidate(candidate, self.min_usd, self.max_usd)
        if verdict.hopeless:
            self.ruled_out += 1
            self.ruled_out_reasons[verdict.hopeless] = (
                self.ruled_out_reasons.get(verdict.hopeless, 0) + 1)
            for k in keys:
                self._done.add(k)
            return False

        for k in keys:
            self._index[k] = candidate
        self._queue[keys[0]] = candidate
        return True

    def add_many(self, candidates: list[Candidate], *, skip_known: bool = True) -> int:
        return sum(1 for c in candidates if self.add(c, skip_known=skip_known))

    # Lower sorts first.
    _KIND_RANK = {"headline": 0, "company_site": 1, "article": 2, "link_hub": 3, "unknown": 4}

    def _priority(self, c: Candidate) -> tuple:
        """Best-evidenced candidates first.

        Run 12 researched 80 companies in arrival order and only 5 cleared the
        four non-email gates - while the headlines that found them had already
        said which ones could. Spending the same budget on the most promising
        candidates first is the difference between a run that reaches the target
        and one that runs out of credits proving what it already knew.
        """
        from .candidate_extractor import assess_candidate

        promise = assess_candidate(c, self.min_usd, self.max_usd)
        return (-promise.score,
                self._KIND_RANK.get(c.source_kind, 4),
                -len(c.discovered_via),
                0 if c.domain else 1)

    def pop_batch(self, n: int) -> list[Candidate]:
        ordered = sorted(self._queue.values(), key=self._priority)[:n]
        for c in ordered:
            self.mark_done(c)
        return ordered

    def mark_done(self, candidate: Candidate) -> None:
        for k in self._keys_for(candidate):
            self._done.add(k)
            self._queue.pop(k, None)
            self._index.pop(k, None)

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def pending(self) -> int:
        return len(self._queue)

    @property
    def processed(self) -> int:
        return len(self._done)
