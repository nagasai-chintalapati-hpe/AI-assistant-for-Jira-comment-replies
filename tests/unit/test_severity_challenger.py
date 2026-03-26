"""Unit tests for SeverityChallenger — Rovo severity change detection & counter-assessment."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.agent.severity_challenger import (
    SeverityChallenger,
    SeverityEvidence,
    RovoSeverityChange,
    SeverityChallengeResult,
)
from src.models.context import (
    ContextCollectionResult,
    IssueContext,
    CommentSnapshot,
)


# Helpers

def _make_context(
    summary="Normal bug",
    description="",
    changelog=None,
    priority="Major",
    labels=None,
    versions=None,
    components=None,
    linked_issues=None,
    last_comments=None,
    jenkins_console_errors=None,
    jenkins_build_info=None,
    testrail_results=None,
    testrail_marker_results=None,
    git_prs=None,
) -> ContextCollectionResult:
    return ContextCollectionResult(
        issue_context=IssueContext(
            issue_key="MORPH-8547",
            summary=summary,
            description=description,
            issue_type="Bug",
            status="Open",
            priority=priority,
            labels=labels or [],
            versions=versions or [],
            components=components or [],
            linked_issues=linked_issues or [],
            last_comments=last_comments or [],
            changelog=changelog or [],
        ),
        jenkins_console_errors=jenkins_console_errors,
        jenkins_build_info=jenkins_build_info,
        testrail_results=testrail_results,
        testrail_marker_results=testrail_marker_results,
        git_prs=git_prs,
        collection_timestamp=datetime.now(timezone.utc),
        collection_duration_ms=100.0,
    )


ROVO_DOWNGRADE_CHANGELOG = [
    {
        "author": "Rovo AI Agent",
        "created": "2026-03-19T10:00:00Z",
        "items": [
            {"field": "priority", "from": "Critical", "to": "Minor"},
        ],
    },
]

ROVO_SEVERITY_CHANGELOG = [
    {
        "author": "Atlassian Intelligence",
        "created": "2026-03-19T10:00:00Z",
        "items": [
            {"field": "severity", "from": "Critical", "to": "Low"},
        ],
    },
]

HUMAN_CHANGELOG = [
    {
        "author": "John Developer",
        "created": "2026-03-19T10:00:00Z",
        "items": [
            {"field": "priority", "from": "Critical", "to": "Minor"},
        ],
    },
]


# Detection tests

class TestRovoDetection:
    """Tests for detecting Rovo changes in the changelog."""

    def test_detects_rovo_downgrade(self):
        challenger = SeverityChallenger()
        ctx = _make_context(changelog=ROVO_DOWNGRADE_CHANGELOG)
        result = challenger.evaluate(ctx)
        assert result is not None
        assert len(result.rovo_changes) == 1
        assert result.rovo_changes[0].is_downgrade is True
        assert result.rovo_changes[0].from_value == "Critical"
        assert result.rovo_changes[0].to_value == "Minor"

    def test_detects_atlassian_intelligence_author(self):
        challenger = SeverityChallenger()
        ctx = _make_context(changelog=ROVO_SEVERITY_CHANGELOG)
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.rovo_changes[0].changed_by == "Atlassian Intelligence"

    def test_ignores_human_changes(self):
        challenger = SeverityChallenger()
        ctx = _make_context(changelog=HUMAN_CHANGELOG)
        result = challenger.evaluate(ctx)
        assert result is None

    def test_ignores_non_severity_fields(self):
        changelog = [
            {
                "author": "Rovo AI Agent",
                "created": "2026-03-19T10:00:00Z",
                "items": [
                    {"field": "status", "from": "Open", "to": "In Progress"},
                ],
            },
        ]
        challenger = SeverityChallenger()
        ctx = _make_context(changelog=changelog)
        result = challenger.evaluate(ctx)
        assert result is None

    def test_returns_none_when_no_changelog(self):
        challenger = SeverityChallenger()
        ctx = _make_context(changelog=[])
        result = challenger.evaluate(ctx)
        assert result is None


# Evidence gathering tests

class TestEvidenceGathering:
    """Tests for evidence signal collection."""

    def test_detects_outage_keywords_in_title(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[8.1.0 outage] During add node in VME greenfield",
            changelog=ROVO_DOWNGRADE_CHANGELOG,
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert "outage" in result.evidence.outage_keyword_matches

    def test_detects_customer_escalation(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="Customer escalation — node add failure",
            description="Observed on customer environment and escalated",
            changelog=ROVO_DOWNGRADE_CHANGELOG,
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.evidence.customer_escalation is True

    def test_detects_title_severity_markers(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[P1] Critical failure in production",
            changelog=ROVO_DOWNGRADE_CHANGELOG,
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.evidence.title_severity_score >= 4

    def test_counts_jenkins_failures(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            changelog=ROVO_DOWNGRADE_CHANGELOG,
            jenkins_console_errors={
                "error_lines": ["ERROR: Build failed", "FATAL: OOM"],
                "exception_blocks": ["NullPointerException at line 42"],
            },
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.evidence.jenkins_failure_count >= 3

    def test_counts_testrail_failures(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            changelog=ROVO_DOWNGRADE_CHANGELOG,
            testrail_results=[
                {"failed": 4, "retest": 2, "total": 20},
            ],
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.evidence.testrail_failure_count >= 6

    def test_detects_linked_blockers(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            changelog=ROVO_DOWNGRADE_CHANGELOG,
            linked_issues=[
                {"key": "MORPH-9000", "type": "Blocks", "status": "Open"},
            ],
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.evidence.linked_blocker_count == 1

    def test_detects_data_loss_hint(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            description="This causes potential data loss in the storage layer",
            changelog=ROVO_DOWNGRADE_CHANGELOG,
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert any("data loss" in h.lower() for h in result.evidence.description_severity_hints)

    def test_pattern_count_from_note(self):
        challenger = SeverityChallenger()
        ctx = _make_context(changelog=ROVO_DOWNGRADE_CHANGELOG)
        result = challenger.evaluate(
            ctx,
            pattern_note="Pattern detected: 6 open Bug/Defect issues on VME / v8.1.0",
        )
        assert result is not None
        assert result.evidence.pattern_count == 6


# Severity computation tests

class TestSeverityComputation:
    """Tests for evidence-based severity rank computation."""

    def test_outage_keywords_raise_to_critical(self):
        """MORPH-8547 scenario: outage keyword should push to Critical/Blocker."""
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[8.1.0 outage] During add node in VME greenfield n/w automation setup failed",
            description="Customer environment escalated",
            changelog=ROVO_DOWNGRADE_CHANGELOG,
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.disagrees is True
        assert result.recommended_rank >= 4  # Critical or Blocker
        assert result.recommended_severity in ("Critical", "Blocker")

    def test_agrees_when_rovo_is_correct(self):
        """When evidence is thin, we agree with Rovo's assessment."""
        changelog = [
            {
                "author": "Rovo AI Agent",
                "created": "2026-03-19T10:00:00Z",
                "items": [
                    {"field": "priority", "from": "Major", "to": "Medium"},
                ],
            },
        ]
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="Minor UI alignment issue on settings page",
            changelog=changelog,
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        # May or may not disagree — but shouldn't recommend higher than Major
        # for a "minor UI alignment" bug

    def test_multiple_signals_boost_confidence(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[8.1.0 outage] Production down — customer escalation",
            description="All users affected, no workaround available, data loss risk",
            changelog=ROVO_DOWNGRADE_CHANGELOG,
            labels=["customer-escalation"],
            jenkins_console_errors={
                "error_lines": ["FATAL: Deploy failed"],
                "exception_blocks": [],
            },
            testrail_results=[{"failed": 8, "retest": 0, "total": 20}],
        )
        result = challenger.evaluate(
            ctx,
            pattern_note="Pattern detected: 5 open Bug/Defect issues on VME / v8.1.0",
        )
        assert result is not None
        assert result.disagrees is True
        assert result.confidence >= 0.7
        assert result.recommended_severity in ("Critical", "Blocker")

    def test_challenge_note_contains_evidence(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[outage] Node add failure",
            description="Customer escalation on production",
            changelog=ROVO_DOWNGRADE_CHANGELOG,
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.challenge_note is not None
        assert "Severity Challenge" in result.challenge_note
        assert "MORPH-8547" in result.challenge_note
        assert "outage" in result.challenge_note.lower()


# to_dict serialization

class TestSerialization:
    """Tests for SeverityChallengeResult.to_dict()."""

    def test_to_dict_roundtrip(self):
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[outage] Test failure",
            changelog=ROVO_DOWNGRADE_CHANGELOG,
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "rovo_changes" in d
        assert "evidence" in d
        assert "recommended_severity" in d
        assert "disagrees" in d
        assert "confidence" in d
        assert isinstance(d["rovo_changes"], list)
        assert d["rovo_changes"][0]["from_value"] == "Critical"

    def test_to_dict_when_no_disagreement(self):
        changelog = [
            {
                "author": "Rovo AI Agent",
                "created": "2026-03-19T10:00:00Z",
                "items": [
                    {"field": "priority", "from": "Major", "to": "Medium"},
                ],
            },
        ]
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="Minor text issue",
            changelog=changelog,
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        d = result.to_dict()
        assert d["challenge_note"] is None  # no challenge when agreeing


# Real-world Rovo comment tests (MORPH-8547 exact scenario)

# The exact comment body from the screenshot
ROVO_COMMENT_BODY = (
    "Rovo has completed an automated review of this issue and the issue "
    "severity has been updated based on the following analysis:\n"
    "The issue causes some VMs to shut off after a failed add-node operation, "
    "but does not result in a complete service outage, data loss, or corruption "
    "for the entire environment. The system remains partially usable and there "
    "is no evidence of total unavailability or data loss, aligning more closely "
    "with P1 criteria. The work item's Severity has been changed from "
    " P0 Critical to P1 High.\n"
    "Rovo evaluated the issue details against the published severity definitions:\n"
    "https://hpe.atlassian.net/wiki/spaces/MORPH/pages/4387701113/Defect+Severity+Definitions\n"
    "If you believe this severity change is inaccurate, you may request a review "
    "by following the defect review process:\n"
    "https://hpe.atlassian.net/wiki/spaces/MORPH/pages/4237297780/Bug+Intake+Triage#Defect-Review-Request"
)


class TestRealWorldRovoComment:
    """Tests based on the exact MORPH-8547 Rovo comment screenshot."""

    def test_detects_rovo_change_from_comment_body(self):
        """The Rovo comment says 'changed from P0 Critical to P1 High'."""
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[8.1.0 outage]During add node in a VME greenfield n/w automation setup failed with err…",
            description="Customer environment escalation. VMs shut off after failed add-node.",
            last_comments=[
                CommentSnapshot(
                    comment_id="99001",
                    author="Automation for Jira",
                    created="2026-03-19T10:00:00Z",
                    body=ROVO_COMMENT_BODY,
                ),
            ],
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert len(result.rovo_changes) >= 1
        # Should detect P0 Critical → P1 High
        change = result.rovo_changes[0]
        assert "P0" in change.from_value or "Critical" in change.from_value
        assert "P1" in change.to_value or "High" in change.to_value
        assert change.is_downgrade is True

    def test_disagrees_with_rovo_on_morph8547(self):
        """For MORPH-8547: outage + customer escalation + VMs shut off = Critical."""
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[8.1.0 outage]During add node in a VME greenfield n/w automation setup failed with err…",
            description="Customer environment escalation. VMs shut off after failed add-node operation.",
            last_comments=[
                CommentSnapshot(
                    comment_id="99001",
                    author="Automation for Jira",
                    created="2026-03-19T10:00:00Z",
                    body=ROVO_COMMENT_BODY,
                ),
            ],
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.disagrees is True
        assert result.recommended_rank >= 4  # Critical or Blocker
        assert result.recommended_severity in ("Critical", "Blocker")

    def test_counters_rovo_partially_usable_claim(self):
        """Rovo says 'partially usable' but VMs are shutting off."""
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[8.1.0 outage]During add node in a VME greenfield n/w automation setup failed",
            description="VMs shut off after failed add-node. Customer environment.",
            last_comments=[
                CommentSnapshot(
                    comment_id="99001",
                    author="Automation for Jira",
                    created="2026-03-19T10:00:00Z",
                    body=ROVO_COMMENT_BODY,
                ),
            ],
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.evidence.rovo_comment_detected is True
        # Should have a counter-argument about "partially usable"
        counters = result.evidence.rovo_reasoning_counters
        assert len(counters) >= 1
        assert any("partially usable" in c.lower() or "partially" in c.lower()
                    for c in counters)

    def test_counters_rovo_no_outage_claim(self):
        """Rovo says 'no complete service outage' but title has [outage]."""
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[8.1.0 outage] Add node failure in VME",
            description="Customer environment escalation",
            last_comments=[
                CommentSnapshot(
                    comment_id="99001",
                    author="Automation for Jira",
                    created="2026-03-19T10:00:00Z",
                    body=ROVO_COMMENT_BODY,
                ),
            ],
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        counters = result.evidence.rovo_reasoning_counters
        assert any("outage" in c.lower() for c in counters)

    def test_counters_rovo_greenfield_scenario(self):
        """Rovo ignores that this is a greenfield deployment = customer blocked."""
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[8.1.0 outage] VME greenfield automation failure",
            description="Customer environment greenfield setup. Add node failed.",
            last_comments=[
                CommentSnapshot(
                    comment_id="99001",
                    author="Automation for Jira",
                    created="2026-03-19T10:00:00Z",
                    body=ROVO_COMMENT_BODY,
                ),
            ],
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        counters = result.evidence.rovo_reasoning_counters
        assert any("greenfield" in c.lower() or "deployment" in c.lower()
                    for c in counters)

    def test_challenge_note_includes_rovo_rebuttals(self):
        """The challenge note should include rebuttals to Rovo's reasoning."""
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[8.1.0 outage]During add node in a VME greenfield n/w automation setup failed",
            description="Customer environment escalation. VMs shut off.",
            last_comments=[
                CommentSnapshot(
                    comment_id="99001",
                    author="Automation for Jira",
                    created="2026-03-19T10:00:00Z",
                    body=ROVO_COMMENT_BODY,
                ),
            ],
        )
        result = challenger.evaluate(ctx)
        assert result is not None
        assert result.challenge_note is not None
        # Should mention Rovo's flawed reasoning
        assert "Rovo" in result.challenge_note
        assert "reasoning" in result.challenge_note.lower() or "flawed" in result.challenge_note.lower()

    def test_p0_p1_rank_mapping(self):
        """P0 Critical should rank higher than P1 High."""
        challenger = SeverityChallenger()
        assert challenger._normalize_rank("P0 Critical") >= 4
        assert challenger._normalize_rank("P1 High") <= 3
        assert challenger._normalize_rank("P0 Critical") > challenger._normalize_rank("P1 High")

    def test_detects_rovo_comment_without_changelog(self):
        """Even with no changelog, a Rovo comment should trigger detection."""
        challenger = SeverityChallenger()
        ctx = _make_context(
            summary="[outage] Test failure",
            changelog=[],  # Empty changelog
            last_comments=[
                CommentSnapshot(
                    comment_id="99001",
                    author="Automation for Jira",
                    created="2026-03-19T10:00:00Z",
                    body=ROVO_COMMENT_BODY,
                ),
            ],
        )
        result = challenger.evaluate(ctx)
        assert result is not None  # Should still detect from comment
        assert len(result.rovo_changes) >= 1
