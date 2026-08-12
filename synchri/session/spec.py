"""The canonical product specification.

The spec is the session's source of truth for what "done" means, and agents may
not quietly redefine it. It is stored verbatim, content-hashed, and versioned:
an agent can write plans, summaries and interpretations all it likes, but a
change to the spec itself produces a new version and therefore a new contract
that every participant has to acknowledge again.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field

from ..errors import ValidationError

MAX_SPEC_CHARS = 500_000

#: Optional structure. Offered, never required -- a user who already has a
#: complete Markdown spec should paste it and move on.
STRUCTURED_FIELDS = (
    ("primary_goal", "Primary goal"),
    ("acceptance_criteria", "Acceptance criteria"),
    ("non_goals", "Non-goals"),
    ("constraints", "Constraints"),
    ("required_technologies", "Required technologies"),
    ("prohibited_technologies", "Prohibited technologies"),
    ("testing_requirements", "Testing requirements"),
    ("security_requirements", "Security requirements"),
)


@dataclass
class ProductSpec:
    """Immutable-by-convention specification text plus optional structure."""

    text: str
    version: int = 1
    structured: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValidationError("the product specification must not be empty")
        if len(self.text) > MAX_SPEC_CHARS:
            raise ValidationError(f"the specification exceeds {MAX_SPEC_CHARS} characters")
        unknown = set(self.structured) - {key for key, _ in STRUCTURED_FIELDS}
        if unknown:
            raise ValidationError(f"unknown specification fields: {', '.join(sorted(unknown))}")

    @property
    def digest(self) -> str:
        """Content hash. Any edit changes this, which is what forces a revision."""
        return hashlib.sha256(self.canonical_text().encode("utf-8")).hexdigest()

    @property
    def short_digest(self) -> str:
        return self.digest[:12]

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def canonical_text(self) -> str:
        """Exactly what goes into the contract: the user's text, plus any structure."""
        blocks = [self.text.strip()]
        extra = [
            f"### {label}\n{self.structured[key].strip()}"
            for key, label in STRUCTURED_FIELDS
            if self.structured.get(key, "").strip()
        ]
        if extra:
            blocks.append("---\n\n" + "\n\n".join(extra))
        return "\n\n".join(blocks)

    def revise(self, text: str | None = None, structured: dict | None = None) -> "ProductSpec":
        """Produce the next version. The old one is never mutated in place."""
        return ProductSpec(
            text=self.text if text is None else text,
            version=self.version + 1,
            structured=self.structured if structured is None else structured,
        )

    def summary(self) -> str:
        return f"Loaded — {self.word_count:,} words (v{self.version}, {self.short_digest})"

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["digest"] = self.digest
        payload["word_count"] = self.word_count
        return payload

    @classmethod
    def from_dict(cls, data: dict | None) -> "ProductSpec | None":
        if not data or not (data.get("text") or "").strip():
            return None
        return cls(
            text=data["text"],
            version=int(data.get("version", 1)),
            structured=dict(data.get("structured") or {}),
        )
