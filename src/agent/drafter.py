"""Draft generator - creates responses using LLM"""

from src.models.comment import Comment
from src.models.classification import CommentClassification
from src.models.context import ContextCollectionResult
from src.models.draft import Draft, DraftStatus
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class ResponseDrafter:
    """Generates draft responses using LLM and templates"""
    
    def __init__(self):
        # TODO: Initialize Anthropic client
        pass
    
    def draft(
        self,
        comment: Comment,
        classification: CommentClassification,
        context: ContextCollectionResult,
    ) -> Draft:
        """
        Generate a draft response to a comment.
        
        Args:
            comment: The comment being responded to
            classification: Classification of the comment
            context: Collected context about the issue
        
        Returns:
            Draft response with citations and metadata
        """
        # TODO: Implement LLM-based draft generation
        # 1. Load appropriate template based on classification
        # 2. Build prompt with context and RAG results
        # 3. Call Claude API
        # 4. Extract draft, actions, labels
        # 5. Generate citations
        
        draft_body = self._generate_response_body(
            comment=comment,
            classification=classification,
            context=context,
        )
        
        draft = Draft(
            draft_id=f"draft_{int(datetime.utcnow().timestamp())}",
            issue_key=comment.issue_key,
            in_reply_to_comment_id=comment.comment_id,
            created_at=datetime.utcnow(),
            created_by="system",
            body=draft_body,
            status=DraftStatus.GENERATED,
            suggested_actions=[],
            suggested_labels=self._suggest_labels(classification),
            confidence_score=classification.confidence,
            citations=[],
        )
        
        return draft
    
    def _generate_response_body(
        self,
        comment: Comment,
        classification: CommentClassification,
        context: ContextCollectionResult,
    ) -> str:
        """Generate the response body using template and LLM"""
        # TODO: Implement actual LLM call
        # For now, return template-based response
        
        if classification.comment_type.value == "cannot_reproduce":
            return (
                f"Thanks for reporting this issue. To help us reproduce it, "
                f"we need a few more details:\n\n"
                f"• Environment (OS, browser, version)\n"
                f"• Step-by-step reproduction steps\n"
                f"• Expected vs actual behavior\n\n"
                f"Once we have this information, we'll be able to investigate further."
            )
        
        return "Thank you for your comment. We're investigating this issue."
    
    def _suggest_labels(self, classification: CommentClassification) -> list[str]:
        """Suggest labels based on classification"""
        labels = []
        
        if classification.missing_context:
            labels.append("needs-info")
        
        if classification.comment_type.value == "cannot_reproduce":
            labels.append("cannot-reproduce")
        
        return labels
