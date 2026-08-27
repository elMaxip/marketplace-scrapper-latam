"""Prices that are not prices, and how to say which ones.

Marketplaces are full of numbers nobody meant as an asking price: the seller who
types 999999 because the form insists on something, the one who types 123456 by
walking down the keyboard, the "1" that means "ask me", the 0 that means the
listing is really an advert for a shop.  All of them pass ``min_price`` and
``max_price`` perfectly well -- 999999 is above any floor and 0 is below any
ceiling -- and each one poisons a different number: 0 becomes the group's
minimum, 999999 becomes its maximum, and both drag the average with them.

So they are excluded *before* the price is compared to anything, which is the
one ordering rule this module has: a junk price is not a cheap listing and not
an expensive one, it is a listing whose price is unknown.

The syntax is deliberately not a regular expression.  What users need to say is
"any run of nines" and "the keyboard walk", and both of those are awkward and
easy to get wrong as a regex -- ``^9+$`` is fine until somebody writes ``9+``
and excludes every price containing a nine.  Six forms, each one a shape:

===========  ==========================================================
``9*``       one digit repeated: 9, 99, 999, 9999, ...
``4>``       the whole ascending run from that digit up to 9: 456789
``4<``       the whole descending run from that digit down to 1: 4321
``4*>``      any ascending run starting there: 4, 45, 456, 4567, ...
``4*<``      any descending run starting there: 4, 43, 432, 4321, ...
``123456``   that exact amount
``gratis``   that exact text, when the marketplace printed words
===========  ==========================================================

Everything numeric is matched against the *amount*, not the text, so "$999.999"
and "999999" and "999 999" are the same number and one pattern catches all
three.  A pattern that is not numeric at all is matched against the text the
marketplace printed, folded for case and accents, because "Gratis" and "gratis"
are the same claim.

Nothing here reads the config or touches a listing: it compiles strings into a
predicate.  That is what lets every rule below be tested against the amounts
that actually turn up rather than through a browser.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

from .utils import price_value

#: What a pattern may end with, and what each ending means.  ``*`` on its own is
#: repetition; the arrows are runs; ``*`` before an arrow makes the run partial.
_REPEAT = "*"
_UP = ">"
_DOWN = "<"


def fold(text: str) -> str:
    """Lower-case, accent-stripped text, for comparing words a human typed.

    "Gratis", "GRATIS" and "gratis" with an accent are one claim written three
    ways, and a filter that only caught the spelling the user happened to type
    would look broken rather than strict.
    """
    decomposed = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _run(start: int, step: int) -> str:
    """The full digit run from ``start``, e.g. ``_run(4, 1) == "456789"``."""
    digits: List[str] = []
    value = start
    while 1 <= value <= 9:
        digits.append(str(value))
        value += step
    return "".join(digits)


def _partial_runs(start: int, step: int) -> Tuple[str, ...]:
    """Every prefix of the full run, shortest first: 4, 45, 456, ..."""
    full = _run(start, step)
    return tuple(full[:length] for length in range(1, len(full) + 1))


class PatternError(ValueError):
    """A pattern the syntax cannot make sense of.

    Raised with the offending pattern in the message, because the web UI shows
    it verbatim and "invalid pattern" without saying which one is useless in a
    list of six.
    """


@dataclass(frozen=True)
class PricePattern:
    """One compiled rule.

    ``digits`` holds the exact digit strings this rule excludes, and is empty
    for a text rule; ``text`` holds the folded word for a text rule, and is
    empty otherwise.  Two fields rather than a subclass hierarchy: there are
    exactly two kinds and they are matched in the same pass.
    """

    source: str
    digits: Tuple[str, ...] = ()
    text: str = ""

    @property
    def is_text(self: "PricePattern") -> bool:
        return not self.digits


def compile_pattern(pattern: str) -> PricePattern:
    """One pattern string as a rule, or raise :class:`PatternError`.

    Order of the checks matters once: the ``*`` forms are recognised before the
    plain-digits form, so ``9*`` is repetition rather than a syntax error about
    a stray asterisk.
    """
    raw = (pattern or "").strip()
    if not raw:
        raise PatternError("A price pattern cannot be empty.")

    body, ending = raw, ""
    # `4*>` is two endings, and the asterisk is the inner one.
    if body.endswith((_UP, _DOWN)):
        ending, body = body[-1], body[:-1]
        if body.endswith(_REPEAT):
            ending, body = _REPEAT + ending, body[:-1]
    elif body.endswith(_REPEAT):
        ending, body = _REPEAT, body[:-1]

    if ending:
        if len(body) != 1 or not body.isdigit():
            raise PatternError(
                f"Price pattern {raw!r}: {ending!r} must follow exactly one digit, "
                "as in '9*', '4>' or '4*<'."
            )
        digit = int(body)
        if digit == 0:
            # There is no run through zero and no interesting repetition of it:
            # "000" is the amount 0, which the exact form already says.
            raise PatternError(f"Price pattern {raw!r}: use '0' on its own for a price of zero.")
        if ending == _REPEAT:
            # Nine repeats is 999999999 -- past any price a marketplace prints,
            # and the point where a longer run stops meaning anything.
            return PricePattern(raw, digits=tuple(body * length for length in range(1, 10)))
        step = 1 if ending.endswith(_UP) else -1
        if ending.startswith(_REPEAT):
            return PricePattern(raw, digits=_partial_runs(digit, step))
        return PricePattern(raw, digits=(_run(digit, step),))

    if body.isdigit():
        # Normalised through int() so "0012" and "12" are one pattern; the
        # amounts they are compared against are normalised the same way.
        return PricePattern(raw, digits=(str(int(body)),))

    folded = fold(body)
    if not folded:
        raise PatternError(f"Price pattern {raw!r} holds nothing to match.")
    return PricePattern(raw, text=folded)


def compile_patterns(patterns: Iterable[str] | None) -> Tuple[PricePattern, ...]:
    """Compile a whole list, reporting *every* bad entry rather than the first.

    A user who mistyped two of six patterns should be told about both; being
    sent back to the form once per mistake is how a form earns its reputation.
    """
    if not patterns:
        return ()
    compiled: List[PricePattern] = []
    errors: List[str] = []
    for pattern in patterns:
        try:
            compiled.append(compile_pattern(pattern))
        except PatternError as error:
            errors.append(str(error))
    if errors:
        raise PatternError(" ".join(errors))
    return tuple(compiled)


def validate_patterns(patterns: Iterable[str] | None) -> List[str]:
    """The problems with a list of patterns, empty when there are none.

    The non-raising face of :func:`compile_patterns`, for the config loader --
    which reports what is wrong with a file rather than failing on the first
    thing it meets.
    """
    problems: List[str] = []
    for pattern in patterns or []:
        if not isinstance(pattern, str):
            problems.append(f"Price pattern {pattern!r} is not a string.")
            continue
        try:
            compile_pattern(pattern)
        except PatternError as error:
            problems.append(str(error))
    return problems


def _digits_of(price: str | None) -> str | None:
    """The amount in a scraped price as a plain digit string, or None.

    Goes through the monitor's own price parser, so a Chilean "450 000", a
    "$999.999" and a discounted "180 000 | 200 000" all read the way they do
    everywhere else.  A price with cents is not junk by these rules -- junk
    prices are round keyboard noise -- so anything with a fractional part is
    reported as unmatchable rather than rounded into a false hit.
    """
    amount = price_value(price)
    if amount is None or amount != int(amount):
        return None
    return str(int(amount))


def matches(price: str | None, patterns: Sequence[PricePattern]) -> PricePattern | None:
    """The first rule that excludes this price, or None when none does.

    The rule is returned rather than a boolean so the caller can say *which*
    pattern threw a listing away; a log line that only says "excluded" leaves
    the user to guess which of their six patterns did it.
    """
    if not patterns:
        return None
    digits = _digits_of(price)
    folded = fold(price or "")
    for pattern in patterns:
        if pattern.is_text:
            if folded and folded == pattern.text:
                return pattern
        elif digits is not None and digits in pattern.digits:
            return pattern
    return None


def is_junk(price: str | None, patterns: Sequence[PricePattern]) -> bool:
    """Whether this price is one the user asked never to count."""
    return matches(price, patterns) is not None
