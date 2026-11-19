"""What this reader gets wrong, learned from the corrections AP staff make.

Every time a person corrects an identifier (NPWP, bank account, tax invoice number), the
pair (what the model read, what is printed) shows one of the model's misreads. Aligned
character by character, those pairs become counts of edit types: `6>0` (a printed 6 read
as 0), `swap` (two neighbouring digits transposed), `ins-dup` (a digit read twice), and so
on.

The counts are used for one thing only: when an NPWP or bank account on an invoice
differs from the vendor master, decide whether the difference *looks like* one of the
reader's known misreads. That changes how the invoice is triaged (check the digits on
the page first), never whether it can be paid: see `matching.decide`.

The model is a plain table of counts, so it is easy to audit ("6 read as 0: seen 14
times"), additive (new corrections just add counts), and versioned with its provenance.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Identifiers made of digits, where a misread is easy to mistake for a changed value.
DIGIT_FIELDS = ("vendor_npwp", "bank_account", "tax_invoice_number")
MAX_EDITS = 2  # a difference needing more edits than this is never called a misread
MIN_COUNT = 3  # an edit type must have been seen this often to count as "known"
MAX_ALIGN = 3  # corrections further apart than this are rewrites, not misreads; skipped

_NON_DIGIT = re.compile(r"\D")


def digits(s: str | None) -> str | None:
    return None if s is None else _NON_DIGIT.sub("", s)


def align(printed: str, read: str) -> list[str] | None:
    """Minimal edits turning `printed` into `read` (optimal string alignment distance).

    Edit names: `a>b` substitution, `swap` adjacent transposition, `ins`/`ins-dup` an extra
    character (dup: same as its neighbour), `del`/`del-dup` a missing one. Returns None
    if the strings are more than MAX_ALIGN edits apart.
    """
    n, m = len(printed), len(read)
    if abs(n - m) > MAX_ALIGN:
        return None
    inf = n + m + 1
    d = [[inf] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if printed[i - 1] == read[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if (
                i > 1
                and j > 1
                and printed[i - 1] == read[j - 2]
                and printed[i - 2] == read[j - 1]
                and printed[i - 1] != printed[i - 2]
            ):
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    if d[n][m] > MAX_ALIGN:
        return None

    edits: list[str] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and printed[i - 1] == read[j - 1] and d[i][j] == d[i - 1][j - 1]:
            i, j = i - 1, j - 1
        elif (
            i > 1
            and j > 1
            and printed[i - 1] == read[j - 2]
            and printed[i - 2] == read[j - 1]
            and d[i][j] == d[i - 2][j - 2] + 1
        ):
            edits.append("swap")
            i, j = i - 2, j - 2
        elif i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + 1:
            edits.append(f"{printed[i - 1]}>{read[j - 1]}")
            i, j = i - 1, j - 1
        elif j > 0 and d[i][j] == d[i][j - 1] + 1:
            near = {read[j - 2] if j > 1 else "", read[j] if j < m else ""}
            edits.append("ins-dup" if read[j - 1] in near else "ins")
            j -= 1
        else:
            near = {printed[i - 2] if i > 1 else "", printed[i] if i < n else ""}
            edits.append("del-dup" if printed[i - 1] in near else "del")
            i -= 1
    return list(reversed(edits))


_EDIT_TEXT = {
    "swap": "two neighbouring digits swapped",
    "ins-dup": "a digit read twice",
    "ins": "an extra digit that is not printed",
    "del-dup": "one of two equal neighbouring digits skipped",
    "del": "a printed digit skipped",
}


def _describe(edit: str) -> str:
    if ">" in edit:
        printed, read = edit.split(">")
        return f"a printed {printed} read as {read}"
    return _EDIT_TEXT.get(edit, edit)


@dataclass(frozen=True)
class Correction:
    field: str
    model_read: str | None
    corrected: str | None


@dataclass(frozen=True)
class Explanation:
    """Why a difference from the master record looks like a misread."""

    edits: tuple[str, ...]
    seen: tuple[int, ...]  # how often each edit was seen in corrections

    def describe(self) -> str:
        return ", ".join(f"{e} (seen {n}x)" for e, n in zip(self.edits, self.seen, strict=True))


@dataclass
class MisreadModel:
    edit_counts: Counter[str] = field(default_factory=Counter)
    n_corrections: int = 0  # identifier corrections used (aligned within MAX_ALIGN)
    n_skipped: int = 0
    min_count: int = MIN_COUNT
    max_edits: int = MAX_EDITS
    trained_on: dict = field(default_factory=dict)  # provenance

    @classmethod
    def fit(cls, corrections: Iterable[Correction], **kw) -> MisreadModel:
        model = cls(**kw)
        for c in corrections:
            model.add(c)
        return model

    def add(self, c: Correction) -> None:
        if c.field not in DIGIT_FIELDS:
            return
        read, printed = digits(c.model_read), digits(c.corrected)
        if not read or not printed or read == printed:
            return
        edits = align(printed, read)
        if edits is None:
            self.n_skipped += 1
            return
        self.n_corrections += 1
        self.edit_counts.update(edits)

    def known(self, edit: str) -> bool:
        return self.edit_counts[edit] >= self.min_count

    def explain(self, read: str | None, master: str) -> Explanation | None:
        """An Explanation if `read` is `master` with only a few, known misreads."""
        read, master = digits(read), digits(master)
        if not read or not master or read == master:
            return None
        edits = align(master, read)
        if not edits or len(edits) > self.max_edits or not all(map(self.known, edits)):
            return None
        return Explanation(tuple(edits), tuple(self.edit_counts[e] for e in edits))

    def hint(self, top: int = 6, min_count: int = 2) -> str | None:
        """The reader's most common misreads, as plain advice for a second read.

        Generic by construction: it names kinds of mistakes, never a value, so it cannot
        steer a reading towards the vendor master.
        """
        phrases = []
        for edit, n in self.edit_counts.most_common():
            if n < min_count or len(phrases) == top:
                break
            phrases.append(f"{_describe(edit)} ({n}x)")
        if not phrases:
            return None
        return (
            "Mistakes this reader has made before on digits, according to staff "
            "corrections: " + "; ".join(phrases) + ". Check for these specifically."
        )

    # ------------------------------------------------------------ persistence

    @property
    def version(self) -> str:
        body = json.dumps(sorted(self.edit_counts.items())).encode()
        return f"misread-n{self.n_corrections}-{hashlib.sha256(body).hexdigest()[:8]}"

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "n_corrections": self.n_corrections,
            "n_skipped": self.n_skipped,
            "min_count": self.min_count,
            "max_edits": self.max_edits,
            "trained_on": self.trained_on,
            "edit_counts": dict(self.edit_counts.most_common()),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> MisreadModel:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            edit_counts=Counter(raw["edit_counts"]),
            n_corrections=raw["n_corrections"],
            n_skipped=raw.get("n_skipped", 0),
            min_count=raw["min_count"],
            max_edits=raw["max_edits"],
            trained_on=raw.get("trained_on", {}),
        )
