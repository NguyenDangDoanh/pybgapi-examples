"""Suggestion rules — plain threshold functions, one file.

Each rule is one small function appended to RULES.  Adding a rule means
adding a function — nothing else changes.  Suggestions are informational
decision-support hints, not clinical predictions.

See design/gateway_app.md.
"""

from __future__ import annotations

import logging
from typing import TypedDict
from dataclasses import dataclass

from .analytics import Analytics

logger = logging.getLogger(__name__)

@dataclass
class Suggestion:
    """A triggered suggestion returned to the dashboard."""

    rule: str   # machine-readable rule id, e.g. "rate_doubled"
    text: str   # human-readable message for the provider


def rule_rate_doubled(client_id: str, analytics: Analytics) -> Suggestion | None:
    """Fire when the last-24h cough rate is more than 2x the prior 24h.

    Returns a Suggestion if triggered, else None.
    """
    current_rate = analytics.rate(client_id, window_h=24)
    prev_rate = analytics.rate_previous(client_id, window_h=24)

    if prev_rate > 0 and current_rate > (2 * prev_rate):
        return Suggestion(
            rule="rate_doubled",
            text=f"Tần suất ho tăng gấp đôi: {current_rate:.1f} lần/giờ (so với {prev_rate:.1f} lần/giờ của 24h trước)."
        )
    
    return None


# Append new rule functions here — evaluate() runs all of them.
RULES = [
    rule_rate_doubled,
]


class Rules:
    """Runs all registered suggestion rules for a given client."""

    def __init__(self, analytics: Analytics) -> None:
        self.analytics = analytics

    def evaluate(self, client_id: str) -> list[Suggestion]:
        """Run every function in RULES; collect non-None results."""
        # Đổi tên thành số nhiều để tránh nhầm lẫn với class Suggestion
        suggestions: list[Suggestion] = []

        for rule_func in RULES:
            try:
                result = rule_func(client_id, self.analytics)
                if result is not None:
                    # Append vào list thay vì gọi từ class
                    suggestions.append(result)
            except Exception as e:
                logger.error(f"Error evaluating rule {rule_func.__name__} for client {client_id}: {e}")

        # Trả về list
        return suggestions