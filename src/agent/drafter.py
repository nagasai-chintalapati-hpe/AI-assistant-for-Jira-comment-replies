"""Draft generator -- creates evidence-based responses from Jira context.

Flow:
1. Extract structured evidence from the collected Jira context
   (build info, environment, errors, timestamps, attachments, logs).
2. Build a data-driven draft using the structured format.
3. Optionally refine with an LLM (Copilot / local).

The goal is a single reply that reduces back-and-forth by citing
real artifacts and asking only the questions the data does not answer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from src.models.classification import CommentClassification, CommentType
from src.models.comment import Comment
from src.models.context import ContextCollectionResult
from src.models.draft import Draft, DraftStatus

logger = logging.getLogger(__name__)

# Evidence-extraction helpers
_ERROR_PATTERNS = re.compile(
    r"(?:error|exception|traceback|failure|failed|fault|500|404|timeout|"
    r"NullPointer|StackOverflow|OutOfMemory|ECONNREFUSED|ECONNRESET|"
    r"Status\s*(?:code)?\s*[:=]\s*[45]\d\d)",
    re.IGNORECASE,
)
_BUILD_PATTERNS = re.compile(
    r"(?:build|version|release|v)\s*[:#=\s]?\s*[\dv][.\d\-_a-zA-Z]*",
    re.IGNORECASE,
)
_ENV_PATTERNS = re.compile(
    r"(?:chrome|firefox|safari|edge|ie)\s*\d+|"
    r"(?:windows|macos|linux|ubuntu|ios|android)\s*[\d.]*|"
    r"(?:staging|production|dev|qa|uat)\s*(?:env(?:ironment)?)?|"
    r"(?:node|python|java|jdk|jre)\s*[\d.]+",
    re.IGNORECASE,
)
_TIMESTAMP_PATTERNS = re.compile(
    r"\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*[A-Z]{2,5})?",
    re.IGNORECASE,
)
_API_PATTERNS = re.compile(
    r"(?:GET|POST|PUT|PATCH|DELETE)\s+/\S+|"
    r"/api/\S+|"
    r"HTTP/\d\.\d\s+\d{3}",
    re.IGNORECASE,
)
_FEATURE_FLAG_PATTERNS = re.compile(
    r"(?:feature[_ ]?flag|ff|toggle)[:\s]+(?!N/?A\b|none\b|null\b)(\S+)|"
    r"([a-z_]+_v\d+)\b",
    re.IGNORECASE,
)

_FF_JUNK = {"n/a", "na", "none", "null", "", "-", "no", "``n/a``", "`n/a`"}


def _extract_matches(pattern, text, limit=5):
    """Return deduplicated regex matches from text."""
    seen = set()
    results = []
    for m in pattern.finditer(text):
        val = m.group(0).strip()
        if val.lower() not in seen:
            seen.add(val.lower())
            results.append(val)
        if len(results) >= limit:
            break
    return results


def _extract_error_lines(text, limit=5):
    """Return lines that look like errors / stack-trace fragments."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip overly long prose lines -- real error lines are usually short
        if len(stripped) > 200:
            continue
        # Skip lines that look like normal sentences (start with capital, end with punctuation)
        if re.match(r'^[A-Z][a-z].{40,}[.!?]$', stripped):
            continue
        if _ERROR_PATTERNS.search(stripped):
            lines.append(stripped[:300])
            if len(lines) >= limit:
                break
    return lines


def _extract_repro_steps(text):
    """Extract numbered or bulleted steps from text."""
    # Template-like headings/labels to skip (Jira description templates)
    _TEMPLATE_RE = re.compile(
        r"(?:environment|browser|os|version|device|platform|"
        r"screenshots?|screen\s*recording|steps?\s*to\s*reproduce|"
        r"expected|actual|severity|priority|impact|workaround|"
        r"exact\s*steps|chrome|firefox|safari|edge)\b",
        re.IGNORECASE,
    )
    steps = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^(?:\d+[.)]\s|[-*]\s|step\s+\d)", stripped, re.IGNORECASE):
            # Get the content after the bullet/number
            content = re.sub(r"^(?:\d+[.)]\s*|[-*]\s*|step\s+\d+[.:)]\s*)", "", stripped)
            # Skip template headings / field labels
            if _TEMPLATE_RE.search(content) and len(content) < 60:
                continue
            # Skip very short labels
            if len(content) < 20:
                continue
            steps.append(stripped)
    return steps[:10]

# Core evidence collection from context
class _Evidence:
    """Structured evidence extracted from Jira context."""

    def __init__(self, comment, classification, context):
        issue = context.issue_context if context else None
        self.issue_key = comment.issue_key
        self.summary = (issue.summary if issue else "") or ""
        self.status = (issue.status if issue else "") or ""
        self.priority = (issue.priority if issue else "") or ""

        description = (issue.description if issue else "") or ""
        environment = (issue.environment if issue else "") or ""

        # Gather ALL text for mining
        all_text_parts = [description, environment, comment.body]
        if issue and issue.last_comments:
            for c in issue.last_comments:
                all_text_parts.append(c.body or "")
        jenkins_snippets = (context.jenkins_log_snippets or {}) if context else {}
        for snippet in jenkins_snippets.values():
            all_text_parts.append(snippet)
        all_text = "\n".join(all_text_parts)

        # --- Build / version ---
        versions = (issue.versions if issue else None) or []
        build_matches = _extract_matches(_BUILD_PATTERNS, all_text, limit=3)
        self.build_versions = list(dict.fromkeys(versions + build_matches))
        self.build_version = self.build_versions[0] if self.build_versions else None

        # --- Environment ---
        self.environment_raw = environment
        self.env_mentions = _extract_matches(_ENV_PATTERNS, all_text, limit=5)
        self.environment = ", ".join(self.env_mentions) if self.env_mentions else (environment or None)

        # --- Errors ---
        self.error_lines = _extract_error_lines(all_text, limit=5)
        self.api_mentions = _extract_matches(_API_PATTERNS, all_text, limit=3)

        # --- Timestamps ---
        self.timestamps = _extract_matches(_TIMESTAMP_PATTERNS, all_text, limit=3)

        # --- Repro steps ---
        self.repro_steps = _extract_repro_steps(description)
        # No fallback — only use real structured steps, not random description lines

        # --- Feature flags ---
        raw_ff = []
        for m in _FEATURE_FLAG_PATTERNS.finditer(all_text):
            val = (m.group(1) or m.group(2) or "").strip().strip("`'\"")
            if val.lower() not in _FF_JUNK and val.lower() not in {v.lower() for v in raw_ff}:
                raw_ff.append(val)
            if len(raw_ff) >= 3:
                break
        self.feature_flags = raw_ff

        # --- Attachments ---
        self.attachments = []
        if issue and issue.attached_files:
            self.attachments = issue.attached_files[:10]
        self.attachment_names = [a.get("filename", "") for a in self.attachments]

        # --- Jenkins ---
        self.jenkins_links = (context.jenkins_links or []) if context else []
        self.jenkins_snippets = jenkins_snippets

        # --- Linked issues ---
        self.linked_issues = []
        if issue and issue.linked_issues:
            self.linked_issues = issue.linked_issues[:5]

        # --- Changelog ---
        self.changelog = []
        if issue and issue.changelog:
            self.changelog = issue.changelog

        # --- Components ---
        self.components = []
        if issue and issue.components:
            self.components = issue.components

        # --- Developer comment (the trigger) ---
        self.dev_comment = comment.body
        self.dev_author = comment.author

        # --- Classification extras ---
        self.missing_context = classification.missing_context or []
        self.suggested_questions = classification.suggested_questions or []
        self.comment_type = classification.comment_type

        # --- Comment thread ---
        self.last_comments = (issue.last_comments if issue else None) or []

# Draft builder
def _section(emoji, title, body):
    """Format a single draft section. Skips empty bodies."""
    if not body or not body.strip():
        return ""
    return emoji + " **" + title + ":**\n" + body + "\n"


def _bullet_list(items):
    return "\n".join("* " + item for item in items)


def _numbered_list(items):
    return "\n".join(str(i + 1) + ". " + item for i, item in enumerate(items))


# Emoji constants (avoids encoding issues in heredocs)
_E_CHECK = "\u2705"
_E_SEARCH = "\U0001f50e"
_E_TEST = "\U0001f9ea"
_E_QUESTION = "\u2753"
_E_NEXT = "\u25b6\ufe0f"


class _DraftBuilder:
    """Builds a structured draft from extracted evidence."""

    def __init__(self, ev):
        self.ev = ev

    def build(self):
        builder_map = {
            CommentType.CANNOT_REPRODUCE: self._cannot_reproduce,
            CommentType.NEED_MORE_INFO: self._need_more_info,
            CommentType.BY_DESIGN: self._by_design,
            CommentType.FIXED_VALIDATE: self._fixed_validate,
            CommentType.OTHER: self._other,
        }
        builder = builder_map.get(self.ev.comment_type, self._other)
        return builder()

    def _cannot_reproduce(self):
        ev = self.ev
        parts = []

        # Acknowledge + restate
        ack = "Thanks, " + ev.dev_author + " -- noted that you are unable to reproduce."
        dev_env = _extract_matches(_ENV_PATTERNS, ev.dev_comment, limit=5)
        if dev_env:
            ack += " Your test environment: " + ", ".join(dev_env) + "."
        parts.append(_section(_E_CHECK, "Acknowledge", ack))

        # Evidence
        evidence_lines = []
        if ev.build_version:
            line = "We can reproduce on **" + ev.build_version + "**"
            if ev.environment:
                line += " in **" + ev.environment + "**"
            line += "."
            evidence_lines.append(line)
        if ev.timestamps:
            evidence_lines.append("Last observed: " + ev.timestamps[0] + ".")
        if ev.error_lines:
            real_errors = [e for e in ev.error_lines[:3] if len(e) < 150]
            if real_errors:
                evidence_lines.append("Observed errors:")
                for err in real_errors:
                    evidence_lines.append("  `" + err + "`")
        if ev.api_mentions:
            evidence_lines.append("API: " + ", ".join("`" + a + "`" for a in ev.api_mentions[:2]))
        if ev.attachment_names:
            evidence_lines.append("Attachments: " + ", ".join(ev.attachment_names[:5]))
        if ev.jenkins_links:
            evidence_lines.append("CI build: " + ev.jenkins_links[0])
        if not evidence_lines:
            evidence_lines.append("See issue description and attached evidence.")
        parts.append(_section(_E_SEARCH, "Evidence", "\n".join(evidence_lines)))

        # Repro steps
        if ev.repro_steps:
            parts.append(_section(_E_TEST, "Repro steps (minimal)", _numbered_list(ev.repro_steps)))

        # Questions
        questions = []
        if not _extract_matches(_BUILD_PATTERNS, ev.dev_comment):
            questions.append("Could you confirm which build/version you tested on?")
        if not dev_env:
            questions.append("What is your test environment (OS, browser, region)?")
        if ev.feature_flags:
            questions.append("Is feature flag `" + ev.feature_flags[0] + "` enabled in your environment?")
        else:
            questions.append("Are there any feature flags or tenant-specific configs that might differ?")
        for q in ev.suggested_questions[:2]:
            if q not in questions:
                questions.append(q)
        if questions:
            parts.append(_section(_E_QUESTION, "Questions", _bullet_list(questions)))

        # Next
        parts.append(_section(_E_NEXT, "Next",
            "If you share your test environment and build, we can validate parity. "
            "Otherwise, please retest on the latest staging build."))

        return "\n".join(p for p in parts if p)

    def _need_more_info(self):
        ev = self.ev
        parts = []

        parts.append(_section(_E_CHECK, "Acknowledge",
            "Thanks for flagging this on **" + ev.issue_key + "** -- we are looking into it."))

        # What we have so far
        evidence_lines = []
        if ev.build_version:
            evidence_lines.append("Build/Version: **" + ev.build_version + "**")
        if ev.environment:
            evidence_lines.append("Environment: " + ev.environment)
        if ev.error_lines:
            evidence_lines.append("Error indicators:")
            for err in ev.error_lines[:3]:
                evidence_lines.append("  `" + err + "`")
        if ev.attachment_names:
            evidence_lines.append("Attachments on file: " + ", ".join(ev.attachment_names[:5]))
        if ev.jenkins_links:
            evidence_lines.append("CI build: " + ev.jenkins_links[0])
        if not evidence_lines:
            evidence_lines.append("Limited evidence on the ticket so far.")
        parts.append(_section(_E_SEARCH, "What we have so far", _bullet_list(evidence_lines)))

        # What we still need
        missing = list(ev.missing_context)
        if not any("log" in m.lower() for m in missing):
            component = ev.components[0] if ev.components else "application"
            missing.append(component + " logs for the last 24 hours")
        if not any("step" in m.lower() for m in missing):
            missing.append("Exact reproduction steps with correlation/request IDs")
        if not any("env" in m.lower() for m in missing):
            missing.append("Environment details (browser, OS, region, tenant)")
        for q in ev.suggested_questions[:2]:
            if q not in missing:
                missing.append(q)
        parts.append(_section(_E_QUESTION, "What we still need", _bullet_list(missing)))

        parts.append(_section(_E_NEXT, "Next",
            "Once we have the above, we can identify the root cause "
            "and provide a timeline for resolution."))

        return "\n".join(p for p in parts if p)

    def _fixed_validate(self):
        ev = self.ev
        parts = []

        parts.append(_section(_E_CHECK, "Acknowledge",
            "A fix has been deployed and is ready for validation."))

        # Fix details
        details = []
        if ev.build_version:
            details.append("Version/Build: **" + ev.build_version + "**")
        target = ev.environment or "staging"
        details.append("Target environment: **" + target + "**")
        if ev.jenkins_links:
            details.append("CI build: " + ev.jenkins_links[0])
        for entry in ev.changelog[-3:]:
            for item in entry.get("items", []):
                if item.get("field") == "status":
                    fr = str(item.get("from", "?"))
                    to = str(item.get("to", "?"))
                    author = str(entry.get("author", "?"))
                    created = str(entry.get("created", "?"))[:10]
                    details.append("Transition: " + fr + " -> " + to + " by " + author + " on " + created)
        parts.append(_section(_E_SEARCH, "Fix details", _bullet_list(details)))

        # Retest checklist
        checklist = []
        if ev.repro_steps:
            checklist.append("Re-run the original repro steps:")
            for step in ev.repro_steps[:5]:
                checklist.append("  " + step)
        checklist.append("Verify fix in **" + target + "**")
        checklist.append("Confirm no regressions in related workflows")
        parts.append(_section(_E_TEST, "Retest checklist", _bullet_list(checklist)))

        parts.append(_section(_E_NEXT, "Next",
            "Please verify in **" + target + "** and update the ticket status. "
            "If the fix does not resolve the issue, reopen with the failing scenario."))

        return "\n".join(p for p in parts if p)

    def _by_design(self):
        ev = self.ev
        parts = []

        parts.append(_section(_E_CHECK, "Acknowledge", "Thanks for raising this."))

        finding = "This is **expected behavior** per the current specification."
        if ev.repro_steps:
            finding += "\n\nThe reported behavior:\n" + _numbered_list(ev.repro_steps[:3])
        parts.append(_section(_E_SEARCH, "Finding", finding))

        parts.append(_section(_E_QUESTION, "Question",
            "If this does not match the acceptance criteria, please point us "
            "to the specific requirement so we can assess whether a doc update "
            "or design change is needed."))

        parts.append(_section(_E_NEXT, "Next",
            "We recommend reviewing the acceptance criteria against the current spec. "
            "Happy to schedule a quick sync if needed."))

        return "\n".join(p for p in parts if p)

    def _other(self):
        ev = self.ev
        parts = []

        parts.append(_section(_E_CHECK, "Acknowledge",
            "Thank you for your comment on **" + ev.issue_key + "** -- " + ev.summary + "."))

        evidence_lines = []
        if ev.error_lines:
            for err in ev.error_lines[:2]:
                evidence_lines.append("`" + err + "`")
        if ev.attachment_names:
            evidence_lines.append("Attachments: " + ", ".join(ev.attachment_names[:3]))
        if evidence_lines:
            parts.append(_section(_E_SEARCH, "Notes", _bullet_list(evidence_lines)))

        parts.append(_section(_E_NEXT, "Next", "We are reviewing this and will follow up shortly."))

        return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# LLM refinement prompt
# ---------------------------------------------------------------------------

_REFINE_SYSTEM = (
    "You are a QA engineer writing a reply on a Jira defect. "
    "Rewrite the draft below to be professional, concise, and empathetic. "
    "Keep all factual data (build numbers, links, error messages) intact. "
    "Do NOT invent facts that are not in the draft. "
    "Output ONLY the refined text -- no markdown fences or explanation."
)
# Public API
class ResponseDrafter:
    """Generates evidence-based draft responses with optional LLM refinement."""

    def __init__(
        self,
        api_key=None,
        model="claude-sonnet-4.5",
        provider=None,
        base_url=None,
        llm_api_key=None,
        github_token=None,
    ):
        self._client = None
        self._model = model
        self._provider = (provider or os.getenv("LLM_PROVIDER", "copilot")).lower()
        self._base_url = (base_url or os.getenv("LLM_BASE_URL", "http://localhost:8080")).rstrip("/")
        self._llm_api_key = llm_api_key or os.getenv("LLM_API_KEY", "")

        token = github_token or api_key
        if self._provider == "copilot" and token:
            try:
                from copilot import CopilotClient
                self._client = CopilotClient({"github_token": token})
                logger.info("Copilot SDK drafter initialized (model=%s)", model)
            except ImportError:
                logger.warning("copilot SDK not available; using template-only mode")
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            logger.info(
                "Local LLM provider enabled for drafter (provider=%s, base=%s)",
                self._provider, self._base_url,
            )

    async def draft(self, comment, classification, context):
        """Generate an evidence-based draft response."""
        # 1. Extract structured evidence from context
        evidence = _Evidence(comment, classification, context)

        # 2. Build draft body from evidence
        content = _DraftBuilder(evidence).build()

        # 3. Build citations from real evidence sources
        citations = self._build_citations(context)

        # 4. Optionally refine with LLM
        if self._provider == "copilot" and self._client:
            refined = await self._refine_with_copilot(content)
            if refined:
                content = refined
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            refined = await self._refine_with_local_llm(content)
            if refined:
                content = refined

        return Draft(
            draft_id="draft_" + uuid.uuid4().hex[:12],
            issue_key=comment.issue_key,
            in_reply_to_comment_id=comment.comment_id,
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body=content,
            status=DraftStatus.GENERATED,
            citations=citations,
            suggested_actions=[{"action": a} for a in self._suggest_actions(classification)],
            confidence_score=classification.confidence,
        )

    async def _refine_with_copilot(self, draft_text):
        """Refine draft using Copilot SDK session."""
        session = None
        try:
            session = await self._client.create_session({
                "model": self._model,
                "available_tools": [],
                "system_message": {
                    "mode": "replace",
                    "content": _REFINE_SYSTEM,
                },
            })

            response = await session.send_and_wait({
                "prompt": draft_text,
            })

            if not response or not response.data or not response.data.content:
                return None

            return response.data.content.strip()
        except Exception as e:
            logger.warning("Copilot refinement failed: %s", e)
            return None
        finally:
            if session:
                try:
                    await session.disconnect()
                except Exception:
                    pass

    async def _refine_with_local_llm(self, draft_text):
        try:
            loop = asyncio.get_running_loop()

            def _do_request():
                headers = {"Content-Type": "application/json"}
                if self._llm_api_key:
                    headers["Authorization"] = "Bearer " + self._llm_api_key
                resp = requests.post(
                    self._base_url + "/v1/chat/completions",
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": _REFINE_SYSTEM},
                            {"role": "user", "content": draft_text},
                        ],
                        "max_tokens": 512,
                        "temperature": 0.3,
                    },
                    headers=headers,
                    timeout=20,
                )
                resp.raise_for_status()
                payload = resp.json()
                return payload["choices"][0]["message"]["content"].strip()

            return await loop.run_in_executor(None, _do_request)
        except Exception as e:
            logger.warning("Local LLM refinement failed: %s", e)
            return None

    @staticmethod
    def _build_citations(context):
        """Build citations list from real evidence sources."""
        citations = []
        issue = context.issue_context if context else None

        if issue and issue.attached_files:
            for att in issue.attached_files[:5]:
                citations.append({
                    "source": "Attachment: " + att.get("filename", "unknown"),
                    "url": att.get("content_url", ""),
                    "excerpt": att.get("mime_type", "") + " (" + str(att.get("size", 0)) + " bytes)",
                })

        if context and context.jenkins_links:
            snippets = context.jenkins_log_snippets or {}
            for link in context.jenkins_links[:3]:
                snippet = snippets.get(link, "")
                excerpt = snippet[-500:] if snippet else "Console output from CI build"
                citations.append({
                    "source": "Jenkins Build Log",
                    "url": link,
                    "excerpt": excerpt,
                })

        if issue and issue.linked_issues:
            for li in issue.linked_issues[:3]:
                citations.append({
                    "source": "Linked Issue: " + li.get("key", ""),
                    "url": "",
                    "excerpt": li.get("type", "") + " - " + li.get("status", ""),
                })

        return citations

    @staticmethod
    def _suggest_actions(classification):
        """Suggest next actions based on classification."""
        mapping = {
            CommentType.CANNOT_REPRODUCE: [
                "Request environment details",
                "Share repro steps",
                "Compare build versions",
            ],
            CommentType.NEED_MORE_INFO: [
                "Request logs with correlation IDs",
                "Ask for exact repro steps",
                "Request environment details",
            ],
            CommentType.FIXED_VALIDATE: [
                "Create retest checklist",
                "Deploy fix to staging",
                "Monitor for regressions",
            ],
            CommentType.BY_DESIGN: [
                "Review acceptance criteria",
                "Update documentation",
                "Schedule design review if needed",
            ],
        }
        return mapping.get(classification.comment_type, [])
