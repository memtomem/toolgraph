"""A whole text withheld for its repr-literal count says so, not "credential-shaped" (#269).

More than ``_MAX_LITERALS`` read-back literals are withheld without being
read: a cost bound, not a finding. The expected strings are written out here
rather than taken from the module's constants, so a constant changed by
mistake fails these tests too.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from toolgraph import log_safety
from toolgraph.error_text import describe_error, describe_os_error
from toolgraph.redaction import display_path, is_credential_shaped, redact_text

CREDENTIAL = "details withheld (they contain credential-shaped data)"
LITERALS = "details withheld (too many escaped literals to check)"
PATH_CREDENTIAL = "<path withheld: credential-shaped>"
PATH_LITERALS = "<path withheld: too many escaped literals to check>"

# One harmless literal exactly as repr prints it: a quote, a, backslash, t, b, a quote.
TAB = "'a\\tb'"


def _literals(literal: str, count: int, sep: str = " ") -> str:
    return sep.join([literal] * count)


SHOWN_PATH = "/srv/" + _literals(TAB, 32, "/") + "/x.yaml"


def _logged(emit) -> list[str]:
    """Lines a filtered handler writes, from a logger outside the hierarchy."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    log_safety.install(handler)
    logger = logging.Logger("toolgraph.test.withheld_reason", logging.DEBUG)
    logger.addHandler(handler)
    emit(logger)
    return stream.getvalue().splitlines()


def test_a_harmless_literal_is_not_credential_shaped():
    assert is_credential_shaped("a\tb") is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cannot parse " + _literals(TAB, 33), LITERALS),
        ("cannot parse " + _literals(TAB, 32), "cannot parse " + _literals(TAB, 32)),
        # Withheld unread: the label names the limit, which is still true.
        ("password=x " + _literals(TAB, 33), LITERALS),
        # The percent-decoded check runs first and keeps its own label.
        ("?%74oken=QXZQ " + _literals(TAB, 33), CREDENTIAL),
        # A literal read back as a credential, under the limit.
        ("'password\\t=QXZQ'", CREDENTIAL),
        ("bad " + _literals(TAB, 31) + " 'password\\t=QXZQ'", CREDENTIAL),
    ],
    ids=["33", "32", "credential-beside-33", "percent-beside-33", "credential", "credential-32"],
)
def test_redact_text_names_the_check_that_withheld(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "literal",
    ["'a\\tb'", "b'\\x00'", "'\\xe9'", "'\\u2028'"],
    ids=["str-tab", "bytes-nul", "ascii-latin", "ascii-line-separator"],
)
@pytest.mark.parametrize("count", [33, 34, 64])
def test_every_read_back_form_over_the_limit_gets_the_limit_label(literal, count):
    assert redact_text(_literals(literal, count)) == LITERALS


def test_a_literal_repr_never_prints_is_not_counted():
    """`'\\u00e9'` is not repr or ascii output (`'é'`, `'\\xe9'`), so it is not read back."""
    text = _literals("'\\u00e9'", 64)

    assert redact_text(text) == text


def test_describe_error_withholds_over_the_limit_with_the_limit_label():
    assert describe_error(ValueError("bad keys " + _literals(TAB, 33))) == LITERALS


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/srv/" + _literals(TAB, 33, "/") + "/x.yaml", PATH_LITERALS),
        (SHOWN_PATH, SHOWN_PATH),
        ("/srv/password=QXZQ/x.yaml", PATH_CREDENTIAL),
    ],
    ids=["33", "32", "credential"],
)
def test_display_path_names_the_check_that_withheld(path, expected):
    assert display_path(path) == expected


@pytest.mark.parametrize("convert", [str, str.encode], ids=["str", "bytes"])
def test_an_os_error_keeps_its_line_and_withholds_only_the_name(convert):
    name = "/srv/" + _literals(TAB, 33, "/") + "/x.yaml"
    exc = FileNotFoundError(2, "No such file or directory", convert(name))

    assert describe_os_error(exc) == (
        "[Errno 2] No such file or directory: '<path withheld: too many escaped literals to check>'"
    )


@pytest.mark.parametrize("marker", [PATH_LITERALS, PATH_CREDENTIAL], ids=["literals", "credential"])
@pytest.mark.parametrize("convert", [str, str.encode, Path], ids=["str", "bytes", "path"])
def test_a_name_that_reads_like_a_marker_is_printed_as_given(marker, convert):
    """Only a name display_path changes is replaced; one that equals a marker
    keeps its type, so bytes and Path names keep their repr."""
    exc = OSError(18, "Invalid cross-device link", convert(marker), None, convert(marker))

    assert describe_os_error(exc) == str(exc)


def test_a_logged_message_over_the_limit_gets_the_limit_label():
    lines = _logged(lambda log: log.info("cannot parse %s", _literals(TAB, 33)))

    assert lines == [LITERALS]


def test_a_logged_exception_over_the_limit_keeps_its_type():
    try:
        raise ValueError("bad keys " + _literals(TAB, 33))
    except ValueError:
        lines = _logged(lambda log: log.exception("boom"))

    assert lines == ["boom", f"ValueError: {LITERALS}"]
