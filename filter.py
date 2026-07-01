from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class FilenameFilter:
    include: str = ""
    exclude: str = ""

    @property
    def is_active(self) -> bool:
        return bool(self.include.strip() or self.exclude.strip())

    def matches(self, filename: str) -> bool:
        name = filename.casefold()

        if self.include.strip() and not matches_filter_expression(name, self.include):
            return False

        if self.exclude.strip() and matches_filter_expression(name, self.exclude):
            return False

        return True


def matches_filter_expression(filename: str, expression: str) -> bool:
    parsed = parse_filter_expression(expression)

    if parsed is None:
        return True

    terms, operators = parsed
    result = _term_matches(filename, terms[0])

    for operator, term in zip(operators, terms[1:]):
        term_result = _term_matches(filename, term)

        if operator == "AND":
            result = result and term_result
        else:
            result = result or term_result

    return result


def parse_filter_expression(expression: str) -> Optional[tuple[List[str], List[str]]]:
    expression = expression.strip()

    if not expression:
        return None

    parts = expression.split()
    terms: List[str] = []
    operators: List[str] = []
    current: List[str] = []

    for part in parts:
        if part in {"AND", "OR"}:
            if not current:
                current.append(part)
                continue

            terms.append(" ".join(current).casefold())
            operators.append(part)
            current = []
        else:
            current.append(part)

    if current:
        terms.append(" ".join(current).casefold())

    if not terms:
        return None

    if len(operators) >= len(terms):
        terms[-1] = f"{terms[-1]} {' '.join(operators[len(terms) - 1:]).casefold()}".strip()
        operators = operators[: len(terms) - 1]

    return terms, operators


def _term_matches(filename: str, term: str) -> bool:
    return term in filename
