"""Defect Validator — validate required fields on new QA defects and flag abnormalities."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.models.context import ContextCollectionResult

logger = logging.getLogger(__name__)


# Required fields that every defect should have populated
_REQUIRED_FIELDS = [
    "summary",
    "description",
    "priority",
    "severity",
    "steps_to_reproduce",
    "environment",
    "affected_version",
]

# Description quality checks — patterns that indicate well-formed defects
_DESCRIPTION_EXPECTED_SECTIONS = [
    (r"(?:steps?\s*to\s*reproduce|reproduction\s*steps|how\s*to\s*reproduce)", "Steps to Reproduce"),
    (r"(?:user\s*impact|impact|business\s*impact)", "User Impact"),
    (r"(?:expected\s*(?:behavior|behaviour|result))", "Expected Behavior"),
    (r"(?:actual\s*(?:behavior|behaviour|result))", "Actual Behavior"),
    (r"(?:frequency|reproducib|occurrence)", "Frequency of Occurrence"),
    (r"(?:service.*version|version|build|release)", "Service Version"),
]

# Abnormality signals
_ABNORMALITY_PATTERNS = [
    # Contradicting priority/severity
    ("priority_severity_mismatch", "Priority and severity are misaligned"),
    # Missing attachments for UI/visual bugs
    ("no_attachments_visual", "Visual/UI defect has no screenshots or attachments"),
    # Vague description
    ("vague_description", "Description is too short or vague (< 50 characters)"),
    # No environment specified
    ("no_environment", "No environment/version information provided"),
]


@dataclass
class ValidationFinding:
    """A single validation finding."""

    field: str
    severity: str  # "error" | "warning" | "info"
    message: str


@dataclass
class DefectValidationResult:
    """Result of validating a new defect raised by QA."""

    issue_key: str
    is_valid: bool
    findings: list[ValidationFinding] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    missing_sections: list[str] = field(default_factory=list)
    abnormalities: list[str] = field(default_factory=list)
    quality_score: float = 0.0  # 0.0 - 1.0

    @property
    def needs_notification(self) -> bool:
        """True when there are errors or abnormalities worth notifying about."""
        return bool(self.abnormalities) or any(
            f.severity == "error" for f in self.findings
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_key": self.issue_key,
            "is_valid": self.is_valid,
            "findings": [
                {"field": f.field, "severity": f.severity, "message": f.message}
                for f in self.findings
            ],
            "missing_fields": self.missing_fields,
            "missing_sections": self.missing_sections,
            "abnormalities": self.abnormalities,
            "quality_score": self.quality_score,
        }

    def summary_text(self) -> str:
        """Human-readable summary for notifications."""
        lines = [f"**Defect Validation — {self.issue_key}**", ""]

        if self.is_valid:
            lines.append("Defect information is complete and well-formed.")
        else:
            lines.append("Issues found with the defect information:")

        if self.missing_fields:
            lines.append("")
            lines.append("**Missing Required Fields:**")
            for mf in self.missing_fields:
                lines.append(f"  • {mf}")

        if self.missing_sections:
            lines.append("")
            lines.append("**Missing Description Sections:**")
            for ms in self.missing_sections:
                lines.append(f"  • {ms}")

        if self.abnormalities:
            lines.append("")
            lines.append("**Abnormalities Detected:**")
            for ab in self.abnormalities:
                lines.append(f"{ab}")

        lines.append("")
        lines.append(f"**Quality Score:** {self.quality_score:.0%}")

        return "\n".join(lines)


class DefectValidator:
    """Validates information provided in new defects raised by QA."""

    def __init__(self, severity_field_id: Optional[str] = None):
        import os
        self._severity_field_id = severity_field_id or os.getenv(
            "JIRA_SEVERITY_FIELD_ID", "customfield_12633"
        )

    def validate(
        self,
        context: ContextCollectionResult,
        issue_fields: Optional[dict[str, Any]] = None,
    ) -> DefectValidationResult:
        """Run all validation checks on a defect.

        Parameters
        ----------
        context : ContextCollectionResult
            The collected context for the issue.
        issue_fields : dict | None
            Raw Jira issue fields (if available) for checking custom fields.
        """
        ctx = context.issue_context
        findings: list[ValidationFinding] = []
        missing_fields: list[str] = []
        missing_sections: list[str] = []
        abnormalities: list[str] = []

        # 1. Check required fields
        self._check_required_fields(ctx, issue_fields, findings, missing_fields)

        # 2. Check description quality and expected sections
        self._check_description_sections(ctx, findings, missing_sections)

        # 3. Check for abnormalities
        self._check_abnormalities(ctx, issue_fields, findings, abnormalities)

        # 4. Compute quality score
        quality_score = self._compute_quality_score(
            ctx, missing_fields, missing_sections, abnormalities
        )

        is_valid = (
            not any(f.severity == "error" for f in findings)
            and not abnormalities
        )

        result = DefectValidationResult(
            issue_key=ctx.issue_key,
            is_valid=is_valid,
            findings=findings,
            missing_fields=missing_fields,
            missing_sections=missing_sections,
            abnormalities=abnormalities,
            quality_score=quality_score,
        )

        if not is_valid:
            logger.warning(
                "Defect validation failed for %s — %d findings, %d abnormalities (score=%.0f%%)",
                ctx.issue_key,
                len(findings),
                len(abnormalities),
                quality_score * 100,
            )
        else:
            logger.info(
                "Defect validation passed for %s (score=%.0f%%)",
                ctx.issue_key,
                quality_score * 100,
            )

        return result

    def _check_required_fields(
        self,
        ctx,
        issue_fields: Optional[dict],
        findings: list[ValidationFinding],
        missing_fields: list[str],
    ) -> None:
        """Check that all required defect fields are populated."""

        # Summary
        if not ctx.summary or len(ctx.summary.strip()) < 10:
            findings.append(ValidationFinding(
                field="summary",
                severity="error",
                message="Summary is missing or too short (< 10 chars)",
            ))
            missing_fields.append("Summary")

        # Description
        if not ctx.description or len(ctx.description.strip()) < 50:
            findings.append(ValidationFinding(
                field="description",
                severity="error",
                message="Description is missing or too short (< 50 chars)",
            ))
            missing_fields.append("Description")

        # Priority
        if not ctx.priority:
            findings.append(ValidationFinding(
                field="priority",
                severity="error",
                message="Priority is not set on the defect",
            ))
            missing_fields.append("Priority")

        # Severity (via context or raw fields)
        severity_value = self._extract_severity(ctx, issue_fields)
        if not severity_value:
            findings.append(ValidationFinding(
                field="severity",
                severity="error",
                message="Severity is not set — required per Defect Severity Classification policy",
            ))
            missing_fields.append("Severity")

        # Environment / version
        has_env = bool(ctx.environment) or bool(ctx.versions)
        if not has_env:
            findings.append(ValidationFinding(
                field="environment",
                severity="warning",
                message="No environment or affected version specified",
            ))
            missing_fields.append("Environment / Affected Version")

        # Components
        if not ctx.components:
            findings.append(ValidationFinding(
                field="components",
                severity="warning",
                message="No component specified — makes triage harder",
            ))

        # Labels
        if not ctx.labels:
            findings.append(ValidationFinding(
                field="labels",
                severity="info",
                message="No labels set on the defect",
            ))

    def _check_description_sections(
        self,
        ctx,
        findings: list[ValidationFinding],
        missing_sections: list[str],
    ) -> None:
        """Check that the description contains expected sections."""
        if not ctx.description:
            return

        desc_lower = ctx.description.lower()

        for pattern, section_name in _DESCRIPTION_EXPECTED_SECTIONS:
            if not re.search(pattern, desc_lower):
                missing_sections.append(section_name)

        if missing_sections:
            findings.append(ValidationFinding(
                field="description",
                severity="warning",
                message=f"Description is missing sections: {', '.join(missing_sections)}",
            ))

    def _check_abnormalities(
        self,
        ctx,
        issue_fields: Optional[dict],
        findings: list[ValidationFinding],
        abnormalities: list[str],
    ) -> None:
        """Detect abnormalities in the defect data."""

        # 1. Priority/Severity mismatch
        severity_value = self._extract_severity(ctx, issue_fields)
        if severity_value and ctx.priority:
            mismatch = self._check_priority_severity_alignment(
                ctx.priority, severity_value
            )
            if mismatch:
                abnormalities.append(mismatch)
                findings.append(ValidationFinding(
                    field="priority_severity",
                    severity="error",
                    message=mismatch,
                ))

        # 2. Visual/UI bug without attachments
        summary_lower = (ctx.summary or "").lower()
        desc_lower = (ctx.description or "").lower()
        is_visual = any(
            kw in summary_lower or kw in desc_lower
            for kw in ["ui", "display", "visual", "screenshot", "layout", "css", "rendering", "font", "color"]
        )
        has_attachments = bool(ctx.attached_files)
        if is_visual and not has_attachments:
            abnormalities.append(
                "Visual/UI defect has no screenshots or attachments — hard to verify"
            )
            findings.append(ValidationFinding(
                field="attachments",
                severity="warning",
                message="UI/visual defect should include screenshots",
            ))

        # 3. High severity but no steps to reproduce
        if severity_value and severity_value.lower() in ("blocker", "critical"):
            if ctx.description and not re.search(
                r"(?:steps?\s*to\s*reproduce|reproduction\s*steps|how\s*to)",
                desc_lower,
            ):
                abnormalities.append(
                    f"Severity is {severity_value} but no Steps to Reproduce provided — "
                    f"critical defects need clear reproduction steps"
                )
                findings.append(ValidationFinding(
                    field="description",
                    severity="error",
                    message="High-severity defect missing Steps to Reproduce",
                ))

        # 4. Summary contains copy-paste boilerplate or is generic
        generic_summaries = ["test", "bug", "defect", "issue", "error", "problem"]
        if ctx.summary and ctx.summary.strip().lower() in generic_summaries:
            abnormalities.append(
                f"Summary is too generic ('{ctx.summary}') — should describe the actual problem"
            )
            findings.append(ValidationFinding(
                field="summary",
                severity="error",
                message="Summary is a generic placeholder — not descriptive",
            ))

        # 5. Labels say one priority but field says another
        if ctx.labels:
            label_priority = self._extract_priority_from_labels(ctx.labels)
            if label_priority and ctx.priority:
                if not self._priorities_align(label_priority, ctx.priority):
                    abnormalities.append(
                        f"Label indicates '{label_priority}' but Priority field is '{ctx.priority}' — contradictory"
                    )
                    findings.append(ValidationFinding(
                        field="labels_vs_priority",
                        severity="warning",
                        message=f"Label '{label_priority}' contradicts Priority field '{ctx.priority}'",
                    ))

    def _extract_severity(self, ctx, issue_fields: Optional[dict]) -> Optional[str]:
        """Extract severity from context or raw issue fields."""
        # Check if severity is in the context (from custom field)
        if hasattr(ctx, "severity") and ctx.severity:
            return ctx.severity

        # Check labels for severity indicators
        for label in (ctx.labels or []):
            label_lower = label.lower()
            if any(s in label_lower for s in ["blocker", "critical", "major", "minor"]):
                return label

        # Check raw issue fields for the custom field
        if issue_fields and self._severity_field_id:
            sev = issue_fields.get(self._severity_field_id)
            if isinstance(sev, dict):
                return sev.get("value", "")
            if isinstance(sev, str):
                return sev

        return None

    @staticmethod
    def _check_priority_severity_alignment(priority: str, severity: str) -> Optional[str]:
        """Check if priority and severity are aligned per classification policy.

        Expected mapping:
            Blocker → P0 / Highest
            Critical → P1 / High
            Major → P2 / Medium
            Minor → P3 / Low
        """
        severity_to_expected_priority = {
            "blocker": {"highest", "p0", "immediate"},
            "critical": {"high", "p1"},
            "major": {"medium", "p2", "normal"},
            "minor": {"low", "p3", "lowest"},
        }

        sev_lower = severity.lower().strip()
        pri_lower = priority.lower().strip()

        # Normalize P1-HIGH style labels
        for key in severity_to_expected_priority:
            if key in sev_lower:
                expected = severity_to_expected_priority[key]
                if not any(e in pri_lower for e in expected):
                    return (
                        f"Severity '{severity}' should map to priority "
                        f"{'/'.join(sorted(expected)).upper()}, but got '{priority}'"
                    )
                return None

        return None

    @staticmethod
    def _extract_priority_from_labels(labels: list[str]) -> Optional[str]:
        """Extract priority indication from labels like 'P1-HIGH'."""
        for label in labels:
            if re.match(r"(?i)^p[0-4]", label):
                return label
        return None

    @staticmethod
    def _priorities_align(label_priority: str, field_priority: str) -> bool:
        """Check if a label like 'P1-HIGH' aligns with field value like 'High'."""
        label_lower = label_priority.lower()
        field_lower = field_priority.lower()

        mapping = {
            "p0": ["highest", "immediate", "blocker"],
            "p1": ["high"],
            "p2": ["medium", "normal"],
            "p3": ["low"],
            "p4": ["lowest", "trivial"],
        }

        for prefix, values in mapping.items():
            if prefix in label_lower:
                return any(v in field_lower for v in values)

        return True  # Can't determine — assume OK

    @staticmethod
    def _compute_quality_score(
        ctx,
        missing_fields: list[str],
        missing_sections: list[str],
        abnormalities: list[str],
    ) -> float:
        """Compute a 0.0-1.0 quality score for the defect."""
        score = 1.0

        # Deductions for missing required fields
        score -= len(missing_fields) * 0.15

        # Deductions for missing description sections
        score -= len(missing_sections) * 0.05

        # Deductions for abnormalities
        score -= len(abnormalities) * 0.15

        # Bonus for having attachments
        if ctx.attached_files:
            score = min(score + 0.05, 1.0)

        return max(0.0, min(score, 1.0))
