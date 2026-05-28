from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from app.normalized import NormalizedMessage

logger = logging.getLogger("ai_serving.compliance")

MAX_RULE_PATTERN_CHARS = 512
MAX_RULES = 200


@dataclass(frozen=True)
class CompiledRule:
    rule_id: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class PolicyMatch:
    rule_id: str
    count: int
    severity: str


@lru_cache(maxsize=32)
def _compile_rules_cached(patterns: tuple[str, ...]) -> tuple[CompiledRule, ...]:
    rules: list[CompiledRule] = []
    if len(patterns) > MAX_RULES:
        logger.warning(
            "Forbidden content rule limit exceeded; extra rules skipped",
            extra={"extra_fields": {"max_rules": MAX_RULES}},
        )

    for entry in patterns[:MAX_RULES]:
        rule_id, separator, regex = entry.partition("=")
        rule_id = rule_id.strip()
        if not separator or not rule_id:
            continue
        # This bounds some obvious ReDoS exposure, but true immunity requires a
        # timeout-capable engine such as regex or subprocess-isolated matching.
        if len(regex) > MAX_RULE_PATTERN_CHARS:
            logger.warning(
                "Forbidden content rule pattern too long; rule skipped",
                extra={
                    "extra_fields": {
                        "rule_id": rule_id,
                        "max_pattern_chars": MAX_RULE_PATTERN_CHARS,
                    }
                },
            )
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
    return tuple(rules)


def compile_rules(patterns: Iterable[str]) -> tuple[CompiledRule, ...]:
    return _compile_rules_cached(tuple(patterns))


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
