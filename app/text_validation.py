"""Reject text that cannot be stored in PostgreSQL or encoded in UTF-8."""


def validate_unicode(value):
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Invalid Unicode text") from None
    return value


def validate_text(value):
    if isinstance(value, str) and "\x00" in value:
        raise ValueError("NUL is not allowed")
    return validate_unicode(value)
