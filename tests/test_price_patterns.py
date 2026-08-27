"""The junk-price syntax, exercised against the amounts marketplaces print.

Every case here is a string a scraper has actually stored or a pattern the help
text under the field promises will work.  No browser and no config: the module
compiles strings into a predicate, so that is what gets tested.
"""

from __future__ import annotations

import pytest

from ai_marketplace_monitor.price_patterns import (
    PatternError,
    compile_pattern,
    compile_patterns,
    is_junk,
    matches,
    validate_patterns,
)


def excluded(price: str, *patterns: str) -> bool:
    return is_junk(price, compile_patterns(patterns))


# --------------------------------------------------------------------------- #
# The six forms, as the help text describes them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("price", ["9", "99", "999", "9999", "999999"])
def test_repeat_matches_every_length(price: str) -> None:
    assert excluded(price, "9*")


@pytest.mark.parametrize("price", ["1", "11", "111", "111111"])
def test_repeat_of_another_digit(price: str) -> None:
    assert excluded(price, "1*")


def test_repeat_does_not_match_a_different_digit() -> None:
    assert not excluded("888", "9*")
    # A real price that merely contains the digit is untouched, which is the
    # failure mode a naive regex has.
    assert not excluded("459990", "9*")


def test_full_ascending_run() -> None:
    assert excluded("456789", "4>")
    # The full run only: a prefix of it is what `4*>` is for.
    assert not excluded("456", "4>")


def test_full_descending_run() -> None:
    assert excluded("4321", "4<")
    assert not excluded("432", "4<")


@pytest.mark.parametrize("price", ["4", "45", "456", "4567", "45678", "456789"])
def test_partial_ascending_runs(price: str) -> None:
    assert excluded(price, "4*>")


@pytest.mark.parametrize("price", ["4", "43", "432", "4321"])
def test_partial_descending_runs(price: str) -> None:
    assert excluded(price, "4*<")


def test_exact_amount() -> None:
    assert excluded("123456", "123456")
    assert not excluded("123457", "123456")


def test_zero_is_expressible() -> None:
    assert excluded("0", "0")


def test_exact_text() -> None:
    assert excluded("Gratis", "gratis")
    assert excluded("GRATIS", "gratis")
    # Accents fold, so one entry covers the spellings people type.
    assert excluded("grátis", "gratis")


def test_text_pattern_is_exact_not_a_substring() -> None:
    # "gratis" in a sentence is a description, not a price, and excluding it
    # would throw away listings whose price is perfectly real.
    assert not excluded("Gratis con despacho", "gratis")


# --------------------------------------------------------------------------- #
# Prices as the scrapers actually store them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "price",
    [
        "$999.999",
        "999 999",
        # Facebook's Chilean listings use a non-breaking space.
        "999 999",
        "CLP 999999",
    ],
)
def test_amount_is_matched_not_the_text(price: str) -> None:
    assert excluded(price, "999999")


def test_discounted_pair_matches_on_the_asking_price() -> None:
    # "current | struck through" -- the first half is what the item costs, and
    # it is the half a filter is about.
    assert excluded("999999 | $1.200.000", "9*")
    assert not excluded("450000 | 999999", "9*")


def test_a_price_with_cents_is_never_junk() -> None:
    # Junk prices are round keyboard noise.  Rounding 999.99 into 999 would
    # throw away a real dollar price on the strength of a pattern about pesos.
    assert not excluded("$999.99", "9*")


def test_unparseable_price_matches_nothing_numeric() -> None:
    assert not excluded("**unspecified**", "9*", "0")


def test_free_text_reads_as_zero() -> None:
    # `price_value` already understands "free"; the numeric rule then applies,
    # so a user who wrote `0` does not also have to write the word.
    assert excluded("Free", "0")


# --------------------------------------------------------------------------- #
# Which pattern did it, and what a bad pattern says
# --------------------------------------------------------------------------- #


def test_matches_reports_the_rule_that_fired() -> None:
    hit = matches("999", compile_patterns(["0", "9*", "gratis"]))
    assert hit is not None and hit.source == "9*"


def test_no_patterns_excludes_nothing() -> None:
    assert not is_junk("999999", ())


@pytest.mark.parametrize("pattern", ["", "  ", "99*", "*", "*>", "0*", "0>", "ab*"])
def test_rejected_patterns(pattern: str) -> None:
    with pytest.raises(PatternError):
        compile_pattern(pattern)


def test_every_bad_entry_is_reported_not_only_the_first() -> None:
    problems = validate_patterns(["9*", "99*", "0>"])
    assert len(problems) == 2
    assert all("Price pattern" in problem for problem in problems)


def test_valid_list_reports_nothing() -> None:
    assert validate_patterns(["9*", "4*>", "123456", "gratis", "0"]) == []


def test_non_string_entry_is_reported_rather_than_crashing() -> None:
    assert validate_patterns([999]) != []  # type: ignore[list-item]


def test_leading_zeros_normalise() -> None:
    assert excluded("12", "0012")


def test_compile_patterns_of_nothing() -> None:
    assert compile_patterns(None) == ()
    assert compile_patterns([]) == ()
