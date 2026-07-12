"""Tests for the shared secret-redaction used by the input filter and output guard."""

from __future__ import annotations

from columbo_py.infra.redaction import redact_secrets


def test_redacts_known_credential_shapes() -> None:
    text = (
        "aws AKIAABCDEFGHIJKLMNOP and slack xoxb-123456789012-abcdefghijkl "
        "and gh ghp_0123456789012345678901234567890123AB and api_key = 's3cr3t-value-here'"
    )
    clean, n = redact_secrets(text)
    assert n >= 4
    assert "AKIAABCDEFGHIJKLMNOP" not in clean
    assert "xoxb-123456789012" not in clean
    assert "ghp_0123456789012345678901234567890123AB" not in clean
    assert "s3cr3t-value-here" not in clean
    assert "api_key = «redacted»" in clean  # key name kept, value redacted


def test_leaves_ordinary_text_untouched() -> None:
    text = "How does token refresh work for the payment-service in staging?"
    clean, n = redact_secrets(text)
    assert n == 0
    assert clean == text


def test_empty_is_safe() -> None:
    assert redact_secrets("") == ("", 0)
