"""Pre-commit hook: block PII patterns in src/apyhiveapi/data/*.json files."""

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

REAL_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
ALLOWED_EMAILS = {"test@test.com"}
PHONE_RE = re.compile(r"\+[0-9]{10,15}")
UK_POSTCODE_RE = re.compile(r"[A-Z]{1,2}[0-9][0-9A-Z]?\s[0-9][A-Z]{2}")
# First octet 1-255 (our anonymised IP starts with "000." which won't match)
REAL_IP_RE = re.compile(r"\b[1-9][0-9]{0,2}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b")

type PatternCheck = tuple[re.Pattern[str], str, Callable[[re.Match[str]], bool]]
PATTERN_CHECKS: list[PatternCheck] = [
    (REAL_UUID_RE, "real UUID", lambda _: True),
    (EMAIL_RE, "real email", lambda m: m.group().lower() not in ALLOWED_EMAILS),
    (PHONE_RE, "phone number", lambda _: True),
    (UK_POSTCODE_RE, "UK postcode", lambda _: True),
    (REAL_IP_RE, "real IP address", lambda _: True),
]

errors: list[str] = []

for filepath in sys.argv[1:]:
    path = Path(filepath)
    if path.suffix != ".json":
        continue

    text = path.read_text(encoding="utf-8")

    # Structure check: the fixture is the raw all-nodes response, an object
    # with "products" and "devices" lists.
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(f"{filepath}: invalid JSON — {exc}")
        continue

    if not isinstance(doc, dict) or not all(
        isinstance(doc.get(key), list) for key in ("products", "devices")
    ):
        found = list(doc) if isinstance(doc, dict) else type(doc).__name__
        errors.append(
            f"{filepath}: file must be an object with "
            f'"products" and "devices" lists (got: {found})'
        )

    # PII pattern checks (run on raw text for speed)
    for lineno, line in enumerate(text.splitlines(), 1):
        for pattern, label, should_flag in PATTERN_CHECKS:
            errors.extend(
                f"{filepath}:{lineno}: {label}: {match.group()!r}"
                for match in pattern.finditer(line)
                if should_flag(match)
            )

if errors:
    print("Data file check FAILED:")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)

print(f"Data file check passed ({len(sys.argv) - 1} file(s) checked)")
sys.exit(0)
