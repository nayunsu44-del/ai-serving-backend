from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.normalized import NormalizedMessage

logger = logging.getLogger("ai_serving.compliance")


@dataclass(frozen=True)
class CompiledRule:
    rule_id: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class PolicyMatch:
    rule_id: str
    count: int
    severity: str


def compile_rules(patterns: Iterable[str]) -> list[CompiledRule]:
    rules: list[CompiledRule] = []
    for entry in patterns:
        rule_id, separator, regex = entry.partition("=")
        rule_id = rule_id.strip()
        if not separator or not rule_id:
            continue
        try:
            pattern = re.compile(regex, re.IGNORECASE)
        except re.error as exc:
            logger.warning(
                "Invalid forbidden content rule skipped",
                extra={
                    "extra_fields": {
                        "rule_id": rule_id,
                        "error_class": type(exc).__name__,
                    }
                },
            )
            continue
        rules.append(CompiledRule(rule_id=rule_id, pattern=pattern))
    return rules


def scan_messages(
    messages: list[NormalizedMessage],
    rules: Iterable[CompiledRule],
) -> list[PolicyMatch]:
    counts: dict[str, int] = {}
    for message in messages:
        for rule in rules:
            match_count = sum(1 for _ in rule.pattern.finditer(message.content))
            if match_count:
                counts[rule.rule_id] = counts.get(rule.rule_id, 0) + match_count

    return [
        PolicyMatch(rule_id=rule_id, count=counts[rule_id], severity="medium")
        for rule_id in sorted(counts)
    ]
