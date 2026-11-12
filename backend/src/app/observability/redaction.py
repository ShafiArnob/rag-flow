import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "private_key",
    "credential",
)
_SENSITIVE_EXACT_KEYS = frozenset({"key"})

_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*")
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key)\b(\s*[=:]\s*)[^\s,;&\"']+"
)
_PROVIDER_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")
# The password in scheme://user:password@host.
_URL_CREDENTIALS_PATTERN = re.compile(r"(://[^\s:/@]*:)[^\s/@]+@")
# A quoted SQL password, as in CREATE ROLE ... PASSWORD '...'. The closing quote is optional so a
# truncated statement is still redacted.
_SQL_PASSWORD_LITERAL_PATTERN = re.compile(r"(?i)\b(PASSWORD\s+)E?'(?:[^']|'')*'?")


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_EXACT_KEYS or any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_text(text: str) -> str:
    text = _URL_CREDENTIALS_PATTERN.sub(lambda match: f"{match[1]}{REDACTED}@", text)
    text = _SQL_PASSWORD_LITERAL_PATTERN.sub(lambda match: f"{match[1]}'{REDACTED}'", text)
    text = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", text)
    text = _ASSIGNMENT_PATTERN.sub(lambda match: f"{match[1]}{match[2]}{REDACTED}", text)
    return _PROVIDER_KEY_PATTERN.sub(REDACTED, text)


def redact_value(value: Any) -> Any:
    """Return a copy of ``value`` with secrets removed; nested containers are walked."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: REDACTED if isinstance(key, str) and is_sensitive_key(key) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact_value(item) for item in value]
    return value
