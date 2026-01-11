"""Query classifier for detecting query/task types.

Classifies user queries into TaskTypes (explain, debug, extend, review, locate)
to enable task-specific context prioritization and prompt templates.
"""

import re
from typing import Tuple

from knowcode.data_models import TaskType


# Pattern definitions for each task type
# Each pattern is a tuple of (compiled_regex, weight)
# Higher weight patterns take precedence

EXPLAIN_PATTERNS = [
    (re.compile(r'\b(explain|how\s+does|how\s+do|walk\s+through|step.by.step|describe|what\s+happens)\b', re.I), 10),
    (re.compile(r'\b(flow|process|mechanism|works?|architecture)\b', re.I), 5),
    (re.compile(r'\b(understand|overview|summary)\b', re.I), 3),
]

DEBUG_PATTERNS = [
    (re.compile(r'\b(debug|bug|error|exception|fail|crash|broken|issue|problem)\b', re.I), 10),
    (re.compile(r'\b(why\s+(is|does|did|are)|what\'?s?\s+wrong|not\s+working|causes?)\b', re.I), 8),
    (re.compile(r'\b(fix|resolve|troubleshoot|diagnose)\b', re.I), 6),
    (re.compile(r'\b(stack\s*trace|traceback|assertion)\b', re.I), 5),
]

EXTEND_PATTERNS = [
    (re.compile(r'\b(add|implement|create|build|extend|modify|change|update)\b', re.I), 8),
    (re.compile(r'\b(how\s+(do\s+I|to|can\s+I|should\s+I))\b', re.I), 6),
    (re.compile(r'\b(where\s+should|best\s+place|pattern|approach)\b', re.I), 5),
    (re.compile(r'\b(new\s+(feature|endpoint|function|class|component))\b', re.I), 7),
]

REVIEW_PATTERNS = [
    (re.compile(r'\b(review|audit|check|analyze|assess)\b', re.I), 10),
    (re.compile(r'\b(change[ds]?|diff|commit|what\'?s?\s+new)\b', re.I), 6),
    (re.compile(r'\b(test\s*coverage|quality|security|performance)\b', re.I), 5),
    (re.compile(r'\b(since|between|compare|versus)\b', re.I), 3),
]

LOCATE_PATTERNS = [
    (re.compile(r'\b(where\s+(is|are|can\s+I\s+find)|find|locate|search)\b', re.I), 10),
    (re.compile(r'\b(defined|declared|implemented|used|called|referenced)\b', re.I), 6),
    (re.compile(r'\b(file|path|location|line)\b', re.I), 4),
    (re.compile(r'\b(show\s+me|list|get)\b', re.I), 3),
]

TASK_PATTERNS = {
    TaskType.EXPLAIN: EXPLAIN_PATTERNS,
    TaskType.DEBUG: DEBUG_PATTERNS,
    TaskType.EXTEND: EXTEND_PATTERNS,
    TaskType.REVIEW: REVIEW_PATTERNS,
    TaskType.LOCATE: LOCATE_PATTERNS,
}


def classify_query(query: str) -> Tuple[TaskType, float]:
    """Classify a query into a TaskType with confidence score.
    
    Args:
        query: User's natural language query.
        
    Returns:
        Tuple of (TaskType, confidence) where confidence is 0.0-1.0.
        Returns (TaskType.GENERAL, 0.0) if no patterns match.
    """
    scores: dict[TaskType, int] = {t: 0 for t in TaskType if t != TaskType.GENERAL}
    
    for task_type, patterns in TASK_PATTERNS.items():
        for pattern, weight in patterns:
            if pattern.search(query):
                scores[task_type] += weight
    
    # Find highest scoring type
    max_score = max(scores.values())
    
    if max_score == 0:
        return TaskType.GENERAL, 0.0
    
    best_type = max(scores, key=scores.get)  # type: ignore
    
    # Calculate confidence based on score relative to maximum possible
    # and gap to second-best
    total_possible = sum(weight for _, weight in TASK_PATTERNS[best_type])
    base_confidence = min(1.0, max_score / (total_possible * 0.5))  # 50% of max = full confidence
    
    # Boost confidence if clear winner (large gap to second place)
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) > 1 and sorted_scores[0] > 0:
        gap_ratio = 1 - (sorted_scores[1] / sorted_scores[0])
        base_confidence = min(1.0, base_confidence * (1 + gap_ratio * 0.3))
    
    return best_type, round(base_confidence, 2)


# System prompt templates for each task type
TASK_PROMPTS = {
    TaskType.EXPLAIN: (
        "You are an expert software engineering assistant explaining code behavior. "
        "Provide a clear, step-by-step explanation of how the code works. "
        "Include relevant code snippets with file paths and line numbers. "
        "Use numbered steps for sequential processes. "
        "Focus on the 'how' and connect components together."
    ),
    TaskType.DEBUG: (
        "You are an expert software engineer debugging an issue. "
        "Focus on: error paths, exception handlers, edge cases, and state mutations. "
        "Identify potential root causes based on the code structure. "
        "Suggest concrete debugging steps or fixes. "
        "Reference specific lines where issues might occur."
    ),
    TaskType.EXTEND: (
        "You are an expert software engineer helping to extend codebase functionality. "
        "Focus on: existing patterns, architectural constraints, related code, and test requirements. "
        "Identify the best location for new code. "
        "Show relevant existing patterns to follow. "
        "Warn about any deprecated patterns to avoid."
    ),
    TaskType.REVIEW: (
        "You are an expert code reviewer analyzing code changes. "
        "Focus on: what changed, test coverage, potential impact, and security concerns. "
        "Identify any breaking changes or regressions. "
        "Note areas that may need additional testing. "
        "Be concise but thorough."
    ),
    TaskType.LOCATE: (
        "You are an expert at navigating codebases. "
        "Provide precise locations: file paths, line numbers, and qualified names. "
        "List all relevant occurrences. "
        "Group by type (definition, usage, test). "
        "Be direct and factual."
    ),
    TaskType.GENERAL: (
        "You are an expert software engineering assistant. "
        "You have access to context from the user's codebase. "
        "Analyze the context and answer the user's question based on your interpretation. "
        "Include code snippets where helpful."
    ),
}


def get_prompt_template(task_type: TaskType) -> str:
    """Get the system prompt template for a task type.
    
    Args:
        task_type: The classified task type.
        
    Returns:
        System prompt string optimized for the task type.
    """
    return TASK_PROMPTS.get(task_type, TASK_PROMPTS[TaskType.GENERAL])
