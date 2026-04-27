"""Severity Challenger — detect Rovo severity changes and counter-assess."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src.models.context import ContextCollectionResult

logger = logging.getLogger(__name__)

# Severity ranking

_SEVERITY_RANK: dict[str, int] = {
    "blocker": 5,
    "p0": 5,
    "p0 critical": 5,
    "critical": 4,
    "highest": 4,
    "p1": 4,
    "p1 high": 3,
    "major": 3,
    "high": 3,
    "p2": 3,
    "p2 medium": 3,
    "medium": 3,
    "normal": 3,
    "minor": 2,
    "p3": 2,
    "p3 low": 2,
    "low": 2,
    "lowest": 2,
    "p4": 2,
    "trivial": 2,
}

# Rovo change patterns
_ROVO_COMMENT_SEVERITY_PATTERNS = [
    # "Severity has been changed from P0 Critical to P1 High"
    re.compile(
        r"severity\s+has\s+been\s+(?:changed|updated)\s+from\s+"
        r"(P\d[\w\s]*?)\s+to\s+(P\d[\w\s]*?)(?:\.|$)",
        re.IGNORECASE,
    ),
    # "priority changed from Critical to Minor"
    re.compile(
        r"(?:priority|severity)\s+(?:changed|updated|modified)\s+from\s+"
        r"(\w[\w\s]*?)\s+to\s+(\w[\w\s]*?)(?:\.|,|$)",
        re.IGNORECASE,
    ),
    # "changed from  P0 Critical to P1 High" (extra whitespace)
    re.compile(
        r"changed\s+from\s+\s*(P\d[\w\s]*?)\s+to\s+(P\d[\w\s]*?)(?:\.|$)",
        re.IGNORECASE,
    ),
]

# Outage keywords

_OUTAGE_KEYWORDS = [
    "outage", "p0", "p1", "sev1", "sev-1", "customer escalation",
    "production down", "prod down", "service down", "critical failure",
    "data loss", "security breach", "security vulnerability",
    "regression", "blocker", "showstopper", "hotfix",
    # Operational / infrastructure keywords (MORPH-8547 type)
    "vms shut off", "vm shut off", "automation failed",
    "automation setup failed", "add node fail", "node failure",
    "deployment fail", "greenfield", "total unavailability",
]

_TITLE_SEVERITY_MARKERS = [
    (re.compile(r"\[.*?outage.*?\]", re.IGNORECASE), 5),
    (re.compile(r"\[.*?p1.*?\]", re.IGNORECASE), 5),
    (re.compile(r"\[.*?critical.*?\]", re.IGNORECASE), 4),
    (re.compile(r"\[.*?sev[- ]?1.*?\]", re.IGNORECASE), 5),
    (re.compile(r"\[.*?sev[- ]?2.*?\]", re.IGNORECASE), 4),
    (re.compile(r"\[.*?regression.*?\]", re.IGNORECASE), 4),
    (re.compile(r"\[.*?hotfix.*?\]", re.IGNORECASE), 4),
]

# Rovo author patterns
_ROVO_AUTHOR_PATTERNS = [
    re.compile(r"rovo", re.IGNORECASE),
    re.compile(r"atlassian\s*intelligence", re.IGNORECASE),
    re.compile(r"automation\s*for\s*jira", re.IGNORECASE),
    re.compile(r"jira\s*automation", re.IGNORECASE),
    re.compile(r"ai[\s-]*agent", re.IGNORECASE),
]

_PRIORITY_BY_SEVERITY = {
    "Blocker": "P0",
    "Critical": "P1",
    "Major": "P2",
    "Minor": "P3",
}

_PRIORITY_CANONICAL = {
    "p0": "P0",
    "highest": "P0",
    "immediate": "P0",
    "showstopper": "P0",
    "p1": "P1",
    "high": "P1",
    "p2": "P2",
    "medium": "P2",
    "normal": "P2",
    "p3": "P3",
    "low": "P3",
    "lowest": "P3",
    "p4": "P3",
}


@dataclass
class RovoSeverityChange:
    """Represents a severity/priority change made by Rovo or an AI agent."""

    changed_by: str
    changed_at: str
    field: str           # "priority" or "severity" (custom field name)
    from_value: str      # e.g. "Critical"
    to_value: str        # e.g. "Minor"
    is_downgrade: bool   # True when to_value ranks lower than from_value


@dataclass
class SeverityEvidence:
    """Evidence signals used to compute recommended severity."""

    outage_keyword_matches: list[str] = field(default_factory=list)
    title_severity_score: int = 0
    pattern_count: int = 0           # open issues on same component/version
    jenkins_failure_count: int = 0
    testrail_failure_count: int = 0
    customer_escalation: bool = False
    linked_blocker_count: int = 0
    affected_version_count: int = 0
    pr_count: int = 0               # related PRs (indicates active development)
    description_severity_hints: list[str] = field(default_factory=list)
    workflow_blocker_terms: list[str] = field(default_factory=list)
    no_workaround_detected: bool = False
    rovo_reasoning_counters: list[str] = field(default_factory=list)  # rebuttals to Rovo's logic
    rovo_comment_detected: bool = False  # True if Rovo change found in comment body


@dataclass
class SeverityPriorityAuditResult:
    """Validation + recommendation for Jira severity and priority fields."""

    criteria_profile: str
    current_severity: str
    current_priority: str
    recommended_severity: str
    recommended_priority: str
    recommended_rank: int
    severity_present: bool
    priority_present: bool
    severity_valid: bool
    priority_valid: bool
    priority_matches_severity: bool
    needs_attention: bool
    findings: list[str]
    evidence: SeverityEvidence
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "criteria_profile": self.criteria_profile,
            "current_severity": self.current_severity,
            "current_priority": self.current_priority,
            "recommended_severity": self.recommended_severity,
            "recommended_priority": self.recommended_priority,
            "recommended_rank": self.recommended_rank,
            "severity_present": self.severity_present,
            "priority_present": self.priority_present,
            "severity_valid": self.severity_valid,
            "priority_valid": self.priority_valid,
            "priority_matches_severity": self.priority_matches_severity,
            "needs_attention": self.needs_attention,
            "findings": self.findings,
            "evidence": {
                "outage_keyword_matches": self.evidence.outage_keyword_matches,
                "title_severity_score": self.evidence.title_severity_score,
                "pattern_count": self.evidence.pattern_count,
                "jenkins_failure_count": self.evidence.jenkins_failure_count,
                "testrail_failure_count": self.evidence.testrail_failure_count,
                "customer_escalation": self.evidence.customer_escalation,
                "linked_blocker_count": self.evidence.linked_blocker_count,
                "affected_version_count": self.evidence.affected_version_count,
                "pr_count": self.evidence.pr_count,
                "description_severity_hints": self.evidence.description_severity_hints,
                "workflow_blocker_terms": self.evidence.workflow_blocker_terms,
                "no_workaround_detected": self.evidence.no_workaround_detected,
            },
            "confidence": self.confidence,
        }


@dataclass
class SeverityChallengeResult:
    """Result of a severity challenge evaluation."""

    rovo_changes: list[RovoSeverityChange]
    evidence: SeverityEvidence
    recommended_severity: str        # "Blocker" | "Critical" | "Major" | …
    recommended_rank: int
    current_rank: int
    disagrees: bool                  # True when we think Rovo got it wrong
    challenge_note: Optional[str]    # Human-readable note for the draft
    confidence: float                # 0.0–1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rovo_changes": [
                {
                    "changed_by": c.changed_by,
                    "changed_at": c.changed_at,
                    "field": c.field,
                    "from_value": c.from_value,
                    "to_value": c.to_value,
                    "is_downgrade": c.is_downgrade,
                }
                for c in self.rovo_changes
            ],
            "evidence": {
                "outage_keyword_matches": self.evidence.outage_keyword_matches,
                "title_severity_score": self.evidence.title_severity_score,
                "pattern_count": self.evidence.pattern_count,
                "jenkins_failure_count": self.evidence.jenkins_failure_count,
                "testrail_failure_count": self.evidence.testrail_failure_count,
                "customer_escalation": self.evidence.customer_escalation,
                "linked_blocker_count": self.evidence.linked_blocker_count,
                "affected_version_count": self.evidence.affected_version_count,
                "pr_count": self.evidence.pr_count,
                "description_severity_hints": self.evidence.description_severity_hints,
                "workflow_blocker_terms": self.evidence.workflow_blocker_terms,
                "no_workaround_detected": self.evidence.no_workaround_detected,
                "rovo_reasoning_counters": self.evidence.rovo_reasoning_counters,
                "rovo_comment_detected": self.evidence.rovo_comment_detected,
            },
            "recommended_severity": self.recommended_severity,
            "recommended_rank": self.recommended_rank,
            "current_rank": self.current_rank,
            "disagrees": self.disagrees,
            "challenge_note": self.challenge_note,
            "confidence": self.confidence,
        }


class SeverityChallenger:
    """Detects Rovo severity changes and produces counter-assessments."""

    def __init__(self, jira_client=None):
        self._jira = jira_client

    def audit_fields(
        self,
        context: ContextCollectionResult,
        pattern_note: Optional[str] = None,
        jira_client=None,
    ) -> SeverityPriorityAuditResult:
        """Validate Jira severity/priority fields and recommend values."""
        jira = jira_client or self._jira
        ctx = context.issue_context
        evidence = self._gather_evidence(context, pattern_note, jira)
        criteria_profile = self._select_criteria_profile(ctx)
        recommended_rank = self._compute_severity_rank(evidence, criteria_profile)
        recommended_severity = self._rank_to_label(recommended_rank)
        recommended_priority = _PRIORITY_BY_SEVERITY[recommended_severity]

        current_severity = (ctx.severity or "").strip()
        current_priority = (ctx.priority or "").strip()
        severity_present = bool(current_severity)
        priority_present = bool(current_priority)

        canonical_severity = self._canonical_severity(current_severity)
        canonical_priority = self._canonical_priority(current_priority)
        severity_valid = canonical_severity is not None
        priority_valid = canonical_priority is not None
        priority_matches_severity = bool(
            canonical_severity and canonical_priority and _PRIORITY_BY_SEVERITY[canonical_severity] == canonical_priority
        )

        findings: list[str] = []
        if not severity_present:
            findings.append("Severity is not set on the Jira issue.")
        elif not severity_valid:
            findings.append(
                f"Severity value '{current_severity}' is not recognised under the configured classification policy."
            )

        if not priority_present:
            findings.append("Priority is not set on the Jira issue.")
        elif not priority_valid:
            findings.append(
                f"Priority value '{current_priority}' does not map cleanly to the P0-P3 policy."
            )

        if canonical_severity and canonical_priority and not priority_matches_severity:
            findings.append(
                f"Severity {canonical_severity} should map to priority {_PRIORITY_BY_SEVERITY[canonical_severity]}, but the issue is currently {canonical_priority}."
            )

        if not findings:
            findings.append(
                f"Severity and priority are set and aligned to the {criteria_profile.replace('_', ' ').title()} criteria."
            )

        if findings[0].startswith("Severity and priority are set"):
            findings.append(
                f"Recommended classification remains {recommended_severity} / {recommended_priority} based on the current evidence signals."
            )
        else:
            findings.append(
                f"Recommended classification is {recommended_severity} / {recommended_priority} based on the current evidence signals."
            )

        return SeverityPriorityAuditResult(
            criteria_profile=criteria_profile,
            current_severity=current_severity,
            current_priority=current_priority,
            recommended_severity=recommended_severity,
            recommended_priority=recommended_priority,
            recommended_rank=recommended_rank,
            severity_present=severity_present,
            priority_present=priority_present,
            severity_valid=severity_valid,
            priority_valid=priority_valid,
            priority_matches_severity=priority_matches_severity,
            needs_attention=not (severity_present and priority_present and severity_valid and priority_valid and priority_matches_severity),
            findings=findings,
            evidence=evidence,
            confidence=self._compute_confidence(evidence, True),
        )

    def evaluate(
        self,
        context: ContextCollectionResult,
        pattern_note: Optional[str] = None,
        jira_client=None,
    ) -> Optional[SeverityChallengeResult]:
        """Run severity challenge evaluation. Returns None if no Rovo change detected."""
        ctx = context.issue_context

        rovo_changes = self._detect_rovo_changes(ctx.changelog or [])
        comment_changes = self._detect_rovo_changes_in_comments(
            ctx.last_comments or []
        )
        rovo_changes.extend(comment_changes)

        if not rovo_changes:
            return None

        logger.info(
            "Detected %d Rovo severity change(s) on %s",
            len(rovo_changes),
            ctx.issue_key,
        )

        audit = self.audit_fields(
            context,
            pattern_note=pattern_note,
            jira_client=jira_client,
        )
        recommended_rank = audit.recommended_rank
        recommended_severity = audit.recommended_severity
        latest_change = rovo_changes[-1]
        current_rank = self._normalize_rank(latest_change.to_value)
        disagrees = recommended_rank > current_rank
        confidence = self._compute_confidence(audit.evidence, disagrees)

        challenge_note = None
        if disagrees:
            challenge_note = self._build_challenge_note(
                rovo_changes,
                audit.evidence,
                recommended_severity,
                latest_change.to_value,
                ctx.issue_key,
            )

        result = SeverityChallengeResult(
            rovo_changes=rovo_changes,
            evidence=audit.evidence,
            recommended_severity=recommended_severity,
            recommended_rank=recommended_rank,
            current_rank=current_rank,
            disagrees=disagrees,
            challenge_note=challenge_note,
            confidence=confidence,
        )

        if disagrees:
            logger.warning(
                "Severity challenge on %s: Rovo set %s → %s, but evidence suggests %s (confidence=%.2f)",
                ctx.issue_key,
                latest_change.from_value,
                latest_change.to_value,
                recommended_severity,
                confidence,
            )

        return result

    # Detection

    def _detect_rovo_changes(
        self, changelog: list[dict[str, Any]]
    ) -> list[RovoSeverityChange]:
        """Scan the Jira changelog for severity/priority changes made by Rovo."""
        changes: list[RovoSeverityChange] = []

        for entry in changelog:
            author = entry.get("author", "")
            if not self._is_rovo_author(author):
                continue

            for item in entry.get("items", []):
                field_name = (item.get("field") or "").lower()
                if field_name not in ("priority", "severity"):
                    continue

                from_val = item.get("from", "") or ""
                to_val = item.get("to", "") or ""
                if not from_val or not to_val:
                    continue

                from_rank = self._normalize_rank(from_val)
                to_rank = self._normalize_rank(to_val)

                changes.append(RovoSeverityChange(
                    changed_by=author,
                    changed_at=entry.get("created", ""),
                    field=field_name,
                    from_value=from_val,
                    to_value=to_val,
                    is_downgrade=to_rank < from_rank,
                ))

        return changes

    @staticmethod
    def _is_rovo_author(author: str) -> bool:
        """Check if the changelog author is Rovo / Atlassian Intelligence."""
        if not author:
            return False
        for pattern in _ROVO_AUTHOR_PATTERNS:
            if pattern.search(author):
                return True
        return False

    def _detect_rovo_changes_in_comments(
        self,
        comments: list,
    ) -> list[RovoSeverityChange]:
        """Scan Jira comments for Rovo severity change announcements.

        Real-world example (from "Automation for Jira"):
            "Rovo has completed an automated review of this issue and the
             issue severity has been updated...
             The work item's Severity has been changed from P0 Critical to P1 High."

        This catches cases where the change appears in a comment body
        even if the changelog detection misses it (field name mismatch, etc.)
        """
        changes: list[RovoSeverityChange] = []

        for comment in comments:
            author = getattr(comment, "author", "") or ""
            body = getattr(comment, "body", "") or ""
            created = getattr(comment, "created", "") or ""

            if not self._is_rovo_author(author):
                # Also check for Rovo-specific phrases in the body
                rovo_body_markers = [
                    "rovo has completed",
                    "automated review of this issue",
                    "severity has been updated",
                    "issue severity has been updated",
                ]
                if not any(m in body.lower() for m in rovo_body_markers):
                    continue

            # Try to extract the from→to severity change from the body
            for pattern in _ROVO_COMMENT_SEVERITY_PATTERNS:
                match = pattern.search(body)
                if match:
                    from_val = match.group(1).strip()
                    to_val = match.group(2).strip()
                    from_rank = self._normalize_rank(from_val)
                    to_rank = self._normalize_rank(to_val)

                    changes.append(RovoSeverityChange(
                        changed_by=author or "Automation for Jira",
                        changed_at=created,
                        field="severity",
                        from_value=from_val,
                        to_value=to_val,
                        is_downgrade=to_rank < from_rank,
                    ))
                    break  # One match per comment is enough

        return changes

    # Evidence gathering

    def _gather_evidence(
        self,
        context: ContextCollectionResult,
        pattern_note: Optional[str],
        jira_client,
    ) -> SeverityEvidence:
        """Collect all signals that inform the recommended severity."""
        ctx = context.issue_context
        ev = SeverityEvidence()

        # 1. Outage keywords in title + description
        text_to_scan = f"{ctx.summary} {ctx.description}".lower()
        for kw in _OUTAGE_KEYWORDS:
            if kw in text_to_scan:
                ev.outage_keyword_matches.append(kw)

        # Check comments too
        for comment in (ctx.last_comments or []):
            body_lower = comment.body.lower()
            for kw in _OUTAGE_KEYWORDS:
                if kw in body_lower and kw not in ev.outage_keyword_matches:
                    ev.outage_keyword_matches.append(kw)

        # 2. Title severity markers (e.g. [8.1.0 outage])
        for pattern, score in _TITLE_SEVERITY_MARKERS:
            if pattern.search(ctx.summary):
                ev.title_severity_score = max(ev.title_severity_score, score)

        # 3. Pattern count (from orchestrator's pattern detection)
        if pattern_note:
            # Extract count from "Pattern detected: N open Bug/Defect..."
            count_match = re.search(r"(\d+)\s+open", pattern_note)
            if count_match:
                ev.pattern_count = int(count_match.group(1))

        # 4. Jenkins failures
        if context.jenkins_console_errors:
            errors = context.jenkins_console_errors
            ev.jenkins_failure_count = (
                len(errors.get("error_lines", []))
                + len(errors.get("exception_blocks", []))
            )
        if context.jenkins_build_info:
            for build in context.jenkins_build_info:
                if build.get("result", "").upper() in ("FAILURE", "ABORTED"):
                    ev.jenkins_failure_count += 1

        # 5. TestRail failures
        for results in (context.testrail_results or []):
            ev.testrail_failure_count += results.get("failed", 0)
            ev.testrail_failure_count += results.get("retest", 0)
        for results in (context.testrail_marker_results or []):
            ev.testrail_failure_count += results.get("failed", 0)

        # 6. Customer escalation detection
        escalation_terms = ["customer", "escalat", "production", "prod env"]
        for term in escalation_terms:
            if term in text_to_scan:
                ev.customer_escalation = True
                break
        # Also check labels
        for label in (ctx.labels or []):
            if any(t in label.lower() for t in ["escalation", "customer", "production"]):
                ev.customer_escalation = True
                break

        # 7. Linked blockers
        for link in (ctx.linked_issues or []):
            link_type = (link.get("type") or "").lower()
            if "block" in link_type:
                ev.linked_blocker_count += 1

        # 8. Affected versions
        ev.affected_version_count = len(ctx.versions or [])

        # 9. PR count (active development = likely real issue)
        ev.pr_count = len(context.git_prs or [])

        # 10. Description severity hints
        desc_lower = ctx.description.lower() if ctx.description else ""
        hint_patterns = [
            (r"data\s*loss", "Potential data loss mentioned"),
            (r"security", "Security concern mentioned"),
            (r"cannot\s+deploy", "Deployment blocker"),
            (r"all\s+users?\s+affected", "All users affected"),
            (r"workaround.*none", "No workaround available"),
            (r"frequency.*always|100%|every\s+time", "Always reproducible"),
        ]
        for pat, hint in hint_patterns:
            if re.search(pat, desc_lower):
                ev.description_severity_hints.append(hint)
                if hint == "No workaround available":
                    ev.no_workaround_detected = True

        workflow_terms = [
            "setup",
            "update",
            "upgrade",
            "cluster expansion",
            "add node",
            "greenfield",
        ]
        for term in workflow_terms:
            if term in text_to_scan and term not in ev.workflow_blocker_terms:
                ev.workflow_blocker_terms.append(term)

        # 11. Rovo reasoning counter-analysis (scan comments for Rovo's logic)
        self._counter_rovo_reasoning(ctx, ev)

        return ev

    @staticmethod
    def _counter_rovo_reasoning(ctx, ev: SeverityEvidence) -> None:
        """Analyse Rovo's stated reasoning and build counter-arguments.

        Real-world example — Rovo says:
          "does not result in a complete service outage, data loss, or
           corruption for the entire environment.  The system remains
           partially usable"

        But our evidence may show VMs shutting off, failed automation in
        a greenfield setup (meaning the customer is blocked), or an
        [outage] tag in the title indicating an actual service impact.
        """
        # Find the Rovo comment body
        rovo_body = ""
        for comment in (ctx.last_comments or []):
            body_lower = (getattr(comment, "body", "") or "").lower()
            if any(m in body_lower for m in [
                "rovo has completed",
                "automated review",
                "severity has been updated",
                "severity has been changed",
            ]):
                rovo_body = body_lower
                ev.rovo_comment_detected = True
                break

        if not rovo_body:
            return

        summary_lower = (ctx.summary or "").lower()
        desc_lower = (ctx.description or "").lower()
        full_text = f"{summary_lower} {desc_lower}"

        # Counter: Rovo says "partially usable" but VMs are shutting off
        if "partially usable" in rovo_body or "remains partially" in rovo_body:
            if any(kw in full_text for kw in ["vm shut", "vms shut", "shut off", "node fail"]):
                ev.rovo_reasoning_counters.append(
                    'Rovo claims "partially usable" but VMs are shutting off — '
                    "partial availability doesn't apply when automation leaves "
                    "infrastructure in a broken state"
                )

        # Counter: Rovo says "no complete service outage" but title has [outage]
        if "no complete service outage" in rovo_body or "not result in a complete" in rovo_body:
            if "outage" in summary_lower:
                ev.rovo_reasoning_counters.append(
                    'Rovo claims "no complete service outage" but the issue '
                    "title contains [outage] — indicates confirmed service impact"
                )

        # Counter: Rovo says "no data loss" but description mentions data loss risk
        if "no evidence of" in rovo_body and "data loss" in rovo_body:
            if "data" in full_text and ("loss" in full_text or "corrupt" in full_text):
                ev.rovo_reasoning_counters.append(
                    "Rovo dismisses data loss risk but the defect description "
                    "mentions data integrity concerns"
                )

        # Counter: Rovo ignores that this is a greenfield/deployment scenario
        if any(kw in full_text for kw in ["greenfield", "add node", "add-node", "deployment"]):
            if "does not result" in rovo_body or "partially" in rovo_body:
                ev.rovo_reasoning_counters.append(
                    "This is a deployment/greenfield scenario — failed automation "
                    "means the customer cannot complete setup, which IS a blocking issue"
                )

        # Counter: Rovo ignores customer escalation context
        if ev.customer_escalation and "customer" not in rovo_body:
            ev.rovo_reasoning_counters.append(
                "Rovo's analysis did not consider that this was escalated "
                "from a customer environment"
            )

        # Counter: Rovo ignores pattern of similar failures
        if ev.pattern_count >= 3:
            ev.rovo_reasoning_counters.append(
                f"Rovo evaluated this issue in isolation but {ev.pattern_count} "
                "similar issues exist on the same component/version — systemic"
            )

    # Severity computation

    def _compute_severity_rank(
        self,
        ev: SeverityEvidence,
        criteria_profile: str = "standard_hpe",
    ) -> int:
        """Compute a recommended severity rank from evidence signals."""
        score = 2

        score = max(score, ev.title_severity_score)

        if len(ev.outage_keyword_matches) >= 3:
            score = max(score, 5)
        elif len(ev.outage_keyword_matches) >= 1:
            score = max(score, 4)

        if ev.customer_escalation:
            score = max(score, 4)

        if ev.pattern_count >= 5:
            score = max(score, 4)
        elif ev.pattern_count >= 3:
            score = max(score, 3)

        if ev.jenkins_failure_count >= 3:
            score = max(score, 3)
        elif ev.jenkins_failure_count >= 1:
            score = max(score, 2)

        if ev.testrail_failure_count >= 5:
            score = max(score, 3)
        elif ev.testrail_failure_count >= 1:
            score = max(score, 2)

        if ev.linked_blocker_count >= 1:
            score = max(score, 3)

        if any("data loss" in h.lower() or "security" in h.lower() for h in ev.description_severity_hints):
            score = max(score, 4)

        if ev.pr_count >= 1:
            score = max(score, 2)

        if criteria_profile == "pcfs":
            if ev.workflow_blocker_terms and (ev.no_workaround_detected or ev.customer_escalation or ev.outage_keyword_matches):
                score = max(score, 5)
            elif ev.workflow_blocker_terms:
                score = max(score, 4)
        else:
            if ev.no_workaround_detected and (
                ev.outage_keyword_matches or ev.linked_blocker_count or any("data loss" in h.lower() for h in ev.description_severity_hints)
            ):
                score = max(score, 5)

        return min(score, 5)

    @staticmethod
    def _compute_confidence(ev: SeverityEvidence, disagrees: bool) -> float:
        """Compute confidence (0.0–1.0) in our severity assessment."""
        if not disagrees:
            return 0.5  # We agree — moderate confidence in status quo

        signals = 0
        total_weight = 0

        # Each signal contributes to confidence
        checks = [
            (len(ev.outage_keyword_matches) > 0, 0.20),
            (ev.title_severity_score >= 4, 0.25),
            (ev.customer_escalation, 0.20),
            (ev.pattern_count >= 3, 0.15),
            (ev.jenkins_failure_count >= 1, 0.10),
            (ev.testrail_failure_count >= 1, 0.10),
            (ev.linked_blocker_count >= 1, 0.08),
            (len(ev.description_severity_hints) > 0, 0.10),
        ]

        for condition, weight in checks:
            total_weight += weight
            if condition:
                signals += weight

        return round(min(signals / 0.5, 1.0), 2) if signals > 0 else 0.1

    # Label helpers

    @staticmethod
    def _normalize_rank(label: str) -> int:
        """Convert a priority/severity label to a numeric rank."""
        return _SEVERITY_RANK.get(label.lower().strip(), 3)

    @staticmethod
    def _rank_to_label(rank: int) -> str:
        """Convert a numeric rank to a human-readable severity label."""
        rank_map = {
            5: "Blocker",
            4: "Critical",
            3: "Major",
            2: "Minor",
            1: "Minor",
            0: "Minor",
        }
        return rank_map.get(rank, "Major")

    @staticmethod
    def _canonical_severity(label: str) -> Optional[str]:
        """Normalise severity values to the supported document taxonomy."""
        normalized = (label or "").strip().lower()
        mapping = {
            "blocker": "Blocker",
            "critical": "Critical",
            "major": "Major",
            "minor": "Minor",
        }
        return mapping.get(normalized)

    @staticmethod
    def _canonical_priority(label: str) -> Optional[str]:
        """Normalise priority values to the P0-P3 taxonomy."""
        normalized = (label or "").strip().lower()
        return _PRIORITY_CANONICAL.get(normalized)

    @staticmethod
    def _select_criteria_profile(ctx) -> str:
        """Pick PCFS rules when the Jira context clearly points to PCFS."""
        parts = [ctx.issue_key, ctx.summary, ctx.description]
        parts.extend(ctx.labels or [])
        parts.extend(ctx.components or [])
        joined = " ".join(part for part in parts if part).lower()
        return "pcfs" if "pcfs" in joined else "standard_hpe"

    # Note generation

    @staticmethod
    def _build_challenge_note(
        rovo_changes: list[RovoSeverityChange],
        evidence: SeverityEvidence,
        recommended: str,
        current: str,
        issue_key: str,
    ) -> str:
        """Build a human-readable severity challenge note."""
        latest = rovo_changes[-1]
        lines = [
            f"🛡️ **Severity Challenge** — {issue_key}",
            f"",
            f"Rovo changed {latest.field} from **{latest.from_value}** → "
            f"**{latest.to_value}**, but evidence suggests "
            f"**{recommended}** is more appropriate.",
            f"",
            f"**Evidence:**",
        ]

        if evidence.outage_keyword_matches:
            kws = ", ".join(f'"{k}"' for k in evidence.outage_keyword_matches[:5])
            lines.append(f"  • Outage/escalation keywords detected: {kws}")

        if evidence.title_severity_score >= 4:
            lines.append(f"  • Title contains high-severity marker (score={evidence.title_severity_score}/5)")

        if evidence.customer_escalation:
            lines.append("  • Customer escalation indicators present")

        if evidence.pattern_count >= 3:
            lines.append(f"  • {evidence.pattern_count} open issues on same component/version (systemic)")

        if evidence.jenkins_failure_count:
            lines.append(f"  • {evidence.jenkins_failure_count} Jenkins build failure(s)")

        if evidence.testrail_failure_count:
            lines.append(f"  • {evidence.testrail_failure_count} TestRail test failure(s)")

        if evidence.linked_blocker_count:
            lines.append(f"  • {evidence.linked_blocker_count} blocking issue link(s)")

        for hint in evidence.description_severity_hints:
            lines.append(f"  • {hint}")

        if evidence.pr_count:
            lines.append(f"  • {evidence.pr_count} related PR(s) — active development")

        # Rovo reasoning rebuttals
        if evidence.rovo_reasoning_counters:
            lines.append("")
            lines.append("**Why Rovo's reasoning is flawed:**")
            for counter in evidence.rovo_reasoning_counters:
                lines.append(f"  ⚠️ {counter}")

        lines.append("")
        lines.append(
            f"**Recommendation:** Maintain/restore **{recommended}** severity. "
            f"Please review before accepting Rovo's downgrade."
        )

        return "\n".join(lines)
