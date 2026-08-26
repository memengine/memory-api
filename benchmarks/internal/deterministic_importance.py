from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

@dataclass(frozen=True, slots=True)
class ImportanceFeatures:
    """Explicit ordinal features; zero means neutral/not reliably derivable."""
    category: str
    temporal_scope: int = 0
    expertise_maturity: int = 0
    goal_commitment: int = 0
    procedure_durability_consequence: int = 0
    identity_breadth: int = 0
    preference_scope: int = 0
    consequence_of_forgetting: int = 0

@dataclass(frozen=True, slots=True)
class DeterministicImportanceResult:
    score: float
    features: ImportanceFeatures
    contributions: dict[str, float]
    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "features": asdict(self.features), "contributions": self.contributions}

class DeterministicImportanceScorer:
    """Transparent zero-provider scorer using proposition and supporting evidence."""
    _patterns = {
        "ephemeral": re.compile(r"\b(today|tonight|for now|this (?:session|week)|next (?:two|three|few) days?|for (?:this|the current) (?:project|session)|temporar(?:y|ily)|until\b|billing failed|invoice (?:fixed|issue)|incident|outage)", re.I),
        "bounded": re.compile(r"\b(this (?:month|quarter|year)|next month|seasonal|audit month)\b", re.I),
        "recurring": re.compile(r"\b(daily|weekly|monthly|every (?:day|week|month|friday|release)|regularly|normally|usually|mostly|always)\b", re.I),
        "durable": re.compile(r"\b(across every project|consistently|permanent(?:ly)?|long[- ]term)\b", re.I),
        "novice": re.compile(r"\b(beginner|new to|only started|cannot yet|can(?:not|'t) build|never (?:used|played))\b", re.I),
        "stale": re.compile(r"\b(have not used|haven't used|not used).{0,30}\b(?:years?|months?)\b|\bstale\b", re.I),
        "senior": re.compile(r"\b(expert|senior|proficient|comfortable with|lead(?:s|ing)? (?:architecture|reviews?)|designed .{0,25} professionally|\d+ years? of professional)\b", re.I),
        "speculative": re.compile(r"\b(someday|would be nice|no (?:current )?plan|maybe|might consider|may consider|considering|thinking about|not committed|not (?:a )?concrete plan)\b", re.I),
        "conditional": re.compile(r"\bif\b.{0,80}\b(might|may|would|consider)\b|\bconditional\b", re.I),
        "active": re.compile(r"\b(active|owns? .{0,30}(?:work|goal|target)|started|working toward)\b", re.I),
        "committed": re.compile(r"\b(registered|committed|four-day|training plan|deadline|within the next (?:year|month)|starts? in \d+ days?|before the next (?:board )?review)\b", re.I),
        "operational_goal": re.compile(r"\b(invoice|billing|ticket|incident|outage|broken|fix(?:ed)?)\b", re.I),
        "workaround": re.compile(r"\b(workaround|manually|until (?:the |a )?patch|next (?:two|three|few) days?)\b", re.I),
        "seasonal": re.compile(r"\b(only during|audit month|seasonal|rest of the year)\b", re.I),
        "routine_procedure": re.compile(r"\b(usually|weekly|monthly|every (?:day|week|month|friday)|morning before meetings)\b", re.I),
        "critical_procedure": re.compile(r"\b(production release|rollback plan|two approvals?|safety|compliance|critical)\b", re.I),
        "temporary_identity": re.compile(r"\b(acting as|temporary|temporarily|until .{0,30} returns?)\b", re.I),
        "stable_identity": re.compile(r"\b(lives? in|normally lives?|works? as|manager|partner|spouse|class \d+|leads? (?:the |an? )?\w+)\b", re.I),
        "foundational_identity": re.compile(r"\b(primary caregiver|core identity|foundational|legal requirement|(?:is|works as) (?:a|an) .{2,35}\bat\b|\bcto\b)\b", re.I),
        "scoped_preference": re.compile(r"\b(for this .{0,25} only|only for|current project|do not care outside|outside this project)\b", re.I),
        "stable_preference": re.compile(r"\b(personal projects?|generally prefer|consistently prefers?|prefers?\b|preference is)\b", re.I),
        "universal_preference": re.compile(r"\b(across every project|always (?:show|use)|in all (?:projects|responses))\b", re.I),
        "high_consequence": re.compile(r"\b(production|rollback|safety|medical|allerg|legal|compliance|manager|deadline|board exam|\bcto\b|leads? (?:the |an? )?\w+|works? as|is (?:a|an) .{2,35}\bat\b|weak in|struggles? with|finds? .{0,35} challenging|owns? .{0,25}migration)\b", re.I),
        "low_consequence": re.compile(r"\b(no (?:current )?plan|do not care|experimenting|workaround|conference this week)\b", re.I),
    }
    _category_prior = {"relationship": .5, "expertise": .25, "goal": .25, "preference": .25}

    def score(self, memory: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> DeterministicImportanceResult:
        content = str(memory.get("content") or memory.get("proposition") or "").strip()
        category = str(memory.get("category") or "fact").strip().lower()
        text = f"{content}\n{self._evidence(memory, messages or [], content)}".strip()
        features = self._derive(category, text)
        contributions = self._contributions(features)
        contributions.update(self._interaction_adjustments(features))
        raw = Decimal("5") + sum((Decimal(str(value)) for value in contributions.values()), Decimal("0"))
        score = float(max(1, min(9, int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))))
        if features.temporal_scope == -2:
            # Session-scoped preferences use the lowest band; other ephemeral
            # memories use the occasional-context anchor.
            score = 1.0 if features.category == "preference" else 3.0
        if (
            features.category == "procedure"
            and features.procedure_durability_consequence == -1
            and features.temporal_scope == 1
            and features.consequence_of_forgetting <= 0
        ):
            # Recurrence alone does not make a low-consequence routine important.
            score = 3.0
        if str(memory.get("disposition") or "").lower() == "pending":
            # Unconfirmed memories cannot receive high durable-memory importance.
            score = min(score, 6.0)
        return DeterministicImportanceResult(score, features, contributions)

    @staticmethod
    def _evidence(memory: dict[str, Any], messages: list[dict[str, Any]], proposition: str) -> str:
        turns = memory.get("evidence_turns") or memory.get("evidence_turn_ids")
        if turns is None:
            evidence = " ".join(str(x.get("content") or "") for x in messages)
            return DeterministicImportanceScorer._relevant_clauses(evidence, proposition)
        selected = []
        for raw in turns:
            try: index = int(raw)
            except (TypeError, ValueError): continue
            if 0 <= index < len(messages): selected.append(str(messages[index].get("content") or ""))
        return DeterministicImportanceScorer._relevant_clauses(" ".join(selected), proposition)

    @staticmethod
    def _relevant_clauses(evidence: str, proposition: str) -> str:
        clauses = [part.strip() for part in re.split(r"(?<=[.!?;])\s+|\s+but\s+|\s+and\s+", evidence, flags=re.I) if part.strip()]
        proposition_terms = {term for term in re.findall(r"[a-z0-9]+", proposition.lower()) if len(term) > 3 and term not in {"user", "their", "with", "this", "that"}}
        if not clauses or not proposition_terms:
            return evidence
        overlaps = [(len(proposition_terms & set(re.findall(r"[a-z0-9]+", clause.lower()))), clause) for clause in clauses]
        best = max(score for score, _ in overlaps)
        return " ".join(clause for score, clause in overlaps if score == best and score > 0) or evidence

    def _has(self, name: str, text: str) -> bool:
        return bool(self._patterns[name].search(text))

    def _derive(self, category: str, text: str) -> ImportanceFeatures:
        temporal = -2 if self._has("ephemeral", text) else -1 if self._has("bounded", text) else 1 if self._has("durable", text) or self._has("recurring", text) else 0
        expertise = -2 if category == "expertise" and (self._has("novice", text) or self._has("stale", text)) else 2 if category == "expertise" and self._has("senior", text) else 0
        goal = -2 if category == "goal" and (self._has("speculative", text) or self._has("conditional", text) or self._has("operational_goal", text)) else 2 if category == "goal" and self._has("committed", text) else 1 if category == "goal" and self._has("active", text) else 0
        procedure = -2 if category == "procedure" and self._has("workaround", text) else -1 if category == "procedure" and (self._has("seasonal", text) or self._has("routine_procedure", text)) else 2 if category == "procedure" and self._has("critical_procedure", text) else 0
        identity = -1 if self._has("temporary_identity", text) else 2 if self._has("foundational_identity", text) else 1 if self._has("stable_identity", text) else 0
        preference = -2 if category == "preference" and self._has("scoped_preference", text) else 2 if category == "preference" and (self._has("universal_preference", text) or self._has("stable_preference", text)) else 0
        consequence = 1 if self._has("high_consequence", text) else -1 if self._has("low_consequence", text) else 0
        return ImportanceFeatures(category, temporal, expertise, goal, procedure, identity, preference, consequence)

    def _contributions(self, f: ImportanceFeatures) -> dict[str, float]:
        return {"category_prior": self._category_prior.get(f.category, 0), "temporal_scope": f.temporal_scope * .75, "expertise_maturity": float(f.expertise_maturity), "goal_commitment": float(f.goal_commitment), "procedure_durability_consequence": float(f.procedure_durability_consequence), "identity_breadth": f.identity_breadth * .5, "preference_scope": float(f.preference_scope), "consequence_of_forgetting": f.consequence_of_forgetting * .5}

    @staticmethod
    def _interaction_adjustments(f: ImportanceFeatures) -> dict[str, float]:
        """General rubric interactions; feature derivation remains independent."""
        return {
            "stable_identity_anchor": 1.0 if f.category == "fact" and f.identity_breadth > 0 and f.temporal_scope <= 0 else 0.0,
            "response_shaping_consequence_anchor": 1.0 if f.temporal_scope <= 0 and f.consequence_of_forgetting > 0 and (f.category == "expertise" or (f.category == "fact" and f.identity_breadth <= 0)) else 0.0,
            "committed_goal_overlap_correction": -0.5 if f.category == "goal" and f.goal_commitment >= 2 and f.consequence_of_forgetting > 0 else 0.0,
        }

__all__ = ["DeterministicImportanceResult", "DeterministicImportanceScorer", "ImportanceFeatures"]
