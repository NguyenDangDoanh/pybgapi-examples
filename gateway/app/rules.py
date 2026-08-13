"""Project-defined statistical findings (not clinical rules)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .analytics import Analytics

logger = logging.getLogger(__name__)


@dataclass
class Suggestion:
    """A project-defined statistical finding returned by the API."""

    rule: str
    text: str
    category: str = "project_defined_statistical_finding"


def rule_rate_doubled(client_id: str, analytics: Analytics) -> Suggestion | None:
    """Report a project-defined two-window rate comparison."""
    current_rate = analytics.rate(client_id, window_h=24)
    previous_rate = analytics.rate_previous(client_id, window_h=24)
    if previous_rate > 0 and current_rate > 2 * previous_rate:
        return Suggestion(
            rule="rate_doubled",
            text=(
                "Cough-bout rate in the last 24 hours is more than twice "
                f"the previous window ({current_rate:.1f} vs "
                f"{previous_rate:.1f} bouts/hour)."
            ),
        )
    return None


def rule_ewma_baseline(
    client_id: str, analytics: Analytics
) -> Suggestion | None:
    """Report when today's observed bout count exceeds its EWMA threshold."""
    status = analytics.ewma_baseline_status(
        client_id=client_id,
        alpha=0.2,
        threshold_pct=0.4,
        min_buffer=5.0,
        warmup_days=7,
    )
    if status["available"] and status["above_baseline"]:
        return Suggestion(
            rule="cough_above_ewma_baseline",
            text=(
                f"Today's observed cough-bout count is {status['today_count']}, "
                f"above the project threshold of {status['threshold']:.1f}. "
                f"The recent personal EWMA baseline is "
                f"{status['baseline']:.1f} bouts/day."
            ),
        )
    return None


RULES = [rule_rate_doubled, rule_ewma_baseline]


class Rules:
    """Run statistical rules independently for one client."""

    def __init__(self, analytics: Analytics) -> None:
        self.analytics = analytics

    def evaluate(self, client_id: str) -> list[Suggestion]:
        findings: list[Suggestion] = []
        for rule_func in RULES:
            try:
                result = rule_func(client_id, self.analytics)
                if result is not None:
                    findings.append(result)
            except Exception:
                logger.exception(
                    "Error evaluating statistical rule %s for client %s",
                    rule_func.__name__,
                    client_id,
                )
        return findings
