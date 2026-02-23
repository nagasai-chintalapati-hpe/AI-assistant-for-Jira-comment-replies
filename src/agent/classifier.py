"""Comment classifier - determines comment intent"""

from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType
import logging

logger = logging.getLogger(__name__)


class CommentClassifier:
    """Classifies developer comments into predefined types"""
    
    def classify(self, comment: Comment) -> CommentClassification:
        """
        Classify a comment based on its content.
        
        Args:
            comment: The comment to classify
        
        Returns:
            CommentClassification with type and confidence
        """
        # TODO: Implement LLM-based classification
        # For now, placeholder using keyword matching
        
        body_lower = comment.body.lower()
        
        # Simple keyword-based classification
        if "cannot reproduce" in body_lower or "can't reproduce" in body_lower:
            return CommentClassification(
                comment_id=comment.comment_id,
                comment_type=CommentType.CANNOT_REPRODUCE,
                confidence=0.85,
                reasoning="Developer indicates inability to reproduce the issue",
                missing_context=["Environment details", "Reproduction steps"],
                suggested_questions=[
                    "What is your environment (OS, browser version)?",
                    "Can you provide step-by-step reproduction steps?",
                ]
            )
        
        elif any(word in body_lower for word in ["logs", "log file", "error log", "stack trace"]):
            return CommentClassification(
                comment_id=comment.comment_id,
                comment_type=CommentType.NEED_MORE_INFO,
                confidence=0.80,
                reasoning="Comment requests logs or diagnostic information",
                missing_context=["Log attachments", "Error messages"],
            )
        
        elif any(word in body_lower for word in ["as designed", "expected behavior", "by design"]):
            return CommentClassification(
                comment_id=comment.comment_id,
                comment_type=CommentType.AS_DESIGNED,
                confidence=0.90,
                reasoning="Comment suggests this is expected behavior",
            )
        
        elif any(word in body_lower for word in ["duplicate", "already fixed", "resolved in"]):
            return CommentClassification(
                comment_id=comment.comment_id,
                comment_type=CommentType.DUPLICATE,
                confidence=0.85,
                reasoning="Comment suggests this is a duplicate or already resolved",
            )
        
        elif any(word in body_lower for word in ["fix ready", "fix deployed", "fix released"]):
            return CommentClassification(
                comment_id=comment.comment_id,
                comment_type=CommentType.FIX_READY,
                confidence=0.88,
                reasoning="Developer indicates a fix is ready for testing",
            )
        
        else:
            return CommentClassification(
                comment_id=comment.comment_id,
                comment_type=CommentType.OTHER,
                confidence=0.50,
                reasoning="Comment type could not be determined with high confidence",
            )
