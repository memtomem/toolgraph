"""A credential printed through ``repr`` reads back for ``redact_text`` (#229).

``repr``, ``ascii``, ``%r``, a container's ``str()`` and library errors such as
``int('…')`` print a tab or a no-break space as an escape (`\\t`, `\\xa0`), so
`password<TAB>=QXZQ` no longer reads as an assignment.
"""

from __future__ import annotations

import time
import tracemalloc

import pytest

from toolgraph.redaction import _MAX_LITERALS, WITHHELD, redact_text

# Separators the scrubber reads as whitespace in raw text and repr escapes.
SEPARATORS = ["\t", "\n", "\x0b", "\xa0", "\u2000", "\u2028", "\u3000"]


def _error(convert, value) -> str:
    try:
        convert(value)
    except ValueError as exc:
        return str(exc)
    raise AssertionError("conversion did not fail")


def _shapes(value: str) -> dict[str, str]:
    data = value.encode()
    return {
        "repr": repr(value),
        "ascii": ascii(value + "\u00a9"),
        "ascii-list": f"{[value + '\u00a9']!a}",
        "list": f"x {[value]!r}",
        "dict-key": str({value: 1}),
        "key-error": str(KeyError(value)),
        "int": _error(int, value),
        "float": _error(float, value),
        "bytes": f"x {data!r}",
        "bytes-after-word": f"x{data!r}",
        "str-after-b": f"b{value!r}",
        "float-bytes": _error(float, data),
        "prose": f"can't read {value!r}, it's odd",
    }


CASES = [
    (f"{name}-{ord(separator):04x}", text)
    for separator in SEPARATORS
    for value in (f"password{separator}=QXZQ", f"password'{separator}=QXZQ\"")
    for name, text in _shapes(value).items()
]


@pytest.mark.parametrize("text", [text for _, text in CASES], ids=[case for case, _ in CASES])
def test_a_repr_printed_credential_is_withheld(text):
    assert redact_text(text) == WITHHELD


@pytest.mark.parametrize(
    "text",
    [
        # The URI scrubber would remove the path holding the name.
        repr("http://h/password\t =QXZQ"),
        # Rewriting `<redacted>` would invalidate the bytes literal.
        repr(b"password\xc2\xa0=QXZQ <redacted>"),
        repr("password\t=QXZQ <redacted>"),
        repr("%74oken\t=QXZQ"),
        _error(float, b"\xc2\xa9Bearer QXZQ"),
    ],
    ids=["uri-path", "bytes-placeholder", "str-placeholder", "percent", "bytes-boundary"],
)
def test_the_message_is_read_back_as_given(text):
    assert redact_text(text) == WITHHELD


@pytest.mark.parametrize(
    "text",
    [
        "C:\\bearer\\notes.json",
        "Path 'C:\\temp\\bearer\\notes.json' does not exist.",
        "cannot read 'C:\\\\bearer\\\\notes.json'",
        "got 'a\\tb'",
        "x b'\\x00\\x01'",
        # Valid escapes, but not what repr prints for the value.
        "'\\x41'",
        # `\U00110000` is a well-formed escape outside Unicode.
        "'\\U00110000'",
    ],
)
def test_text_that_reads_back_harmlessly_is_shown(text):
    assert redact_text(text) == text


@pytest.mark.parametrize("text", ["'\\x70assword\\t=QXZQ'", "b'\\x70assword\\t=QXZQ'"])
def test_a_literal_repr_would_not_print_is_not_read(text):
    """Only repr or ascii output is read back: repr prints `p`, not `\\x70`."""
    assert redact_text(text) == text


def test_a_literal_is_read_in_place_in_the_message():
    # Neither the literal alone nor the raw text is credential-shaped.
    assert redact_text("password" + repr("\t=QXZQ")) == WITHHELD


def test_a_scrubbable_credential_beside_a_read_back_literal_withholds_the_message():
    # The cost of reading the message as given: no scrubbing, the whole text goes.
    assert redact_text("got 'a\\tb' and password=QXZQ") == WITHHELD
    assert redact_text("got 'ab' and password=QXZQ") == "got 'ab' and password=<redacted>"


@pytest.mark.parametrize(
    "text",
    [
        # A quote opened and never closed, then backslash text.
        "can't read C:\\nBearer\\notes.json",
        "got 'safe' C:\\nBearer\\notes.json",
    ],
)
def test_text_after_an_unpaired_quote_is_not_read(text):
    assert redact_text(text) == text


def test_a_literal_cut_without_its_closing_quote_is_not_read():
    """A documented limit: ``int()`` prints at most 200 characters of the repr
    and no closing quote, so there is no literal to read back."""
    text = _error(int, "password\t=QXZQ" + "x" * 250)
    assert not text.endswith("'")

    assert redact_text(text) == text


def test_a_literal_printed_through_repr_twice_is_not_read():
    """A documented limit: only one level of repr is read back."""
    text = repr(repr("password\t=QXZQ"))

    assert redact_text(text) == text


def test_a_literal_after_an_odd_backslash_run_is_not_read():
    """A documented limit: an escaped quote never starts a literal."""
    text = "C:\\" + repr("password\t=QXZQ")

    assert redact_text(text) == text


@pytest.mark.parametrize("item", ["a\tb", b"a\tb"], ids=["str", "bytes"])
def test_more_literals_than_the_limit_are_withheld_unread(item):
    assert redact_text(repr([item] * _MAX_LITERALS)) != WITHHELD
    assert redact_text(repr([item] * (_MAX_LITERALS + 1))) == WITHHELD


def test_reading_back_prints_no_parser_warning(recwarn, capsys):
    for text in (r"b'password\u2000=QXZQ'", r"'\s\d'", r"'\N{SPACE}'", r"b'\777'"):
        redact_text(text)

    assert len(recwarn) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "text",
    [
        repr('"' + "'" * 20_000),
        "\\'" * 20_000,
        "'\\t" * 20_000,
        "'a" * 100_000,
        "\\" * 100_000 + "'",
    ],
    ids=["escaped-quotes-in-literal", "escaped-quotes", "literals", "open-quotes", "backslash-run"],
)
def test_reading_back_stays_linear(text):
    # Sized so that a quadratic scan fails in seconds rather than minutes.
    started = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "text",
    [
        "x" * 1_000_000 + " ".join(repr(b"a\tb") for _ in range(_MAX_LITERALS)),
        repr("x" * 1_000_000),
        repr("x\t" * 500_000),
        "'" + "x" * 1_000_000,
    ],
    ids=["many-literals", "long-literal", "long-escaped-literal", "unclosed"],
)
def test_reading_back_stays_linear_in_memory(text):
    tracemalloc.start()
    try:
        redact_text(text)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Parsing a long literal takes about 9x its length on its own (measured,
    # constant from 0.75 to 3 MB). Holding every form, or a backtracking
    # literal match, costs tens of times more.
    assert peak < 16 * len(text)
