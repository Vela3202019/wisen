"""
Text scrubbing for anything that leaves the machine: secrets, email addresses
and absolute paths are replaced with placeholders. Patterns are the well-known
token shapes (as in gitleaks); scrubbing is a second layer behind the packet
allow-list, never a guarantee on its own.

Placeholders never match a pattern again, so scrubbing twice is a no-op.
"""
import re

_SECRETS = [
    # PEM private key blocks, including ones cut off before the END line
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY-----|.*)", re.DOTALL),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),      # Anthropic, OpenAI
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),             # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),              # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                  # Google API key
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),          # Slack
    re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}\b"),   # Stripe
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),                    # Hugging Face
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),             # Telegram bot token
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]
# Keep the label, mask the value: "Bearer x", "API_KEY=x", "password: x"
_LABELLED = [
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)(\b[\w-]*(?:api[_-]?key|secret|token|passw(?:or)?d)[\w-]*[\"']?\s*[:=]\s*[\"']?)[^\s\"'<>,;]{8,}"),
]
_URL_CREDENTIALS = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Home dirs collapse to "~"; the user name is part of the path.
_HOME = re.compile(r"(?:/Users|/home)/[^/\s\"'`]+|/root\b|\b[A-Za-z]:\\Users\\[^\\\s\"'`]+")
# Any other absolute path (two or more segments) keeps only its last segment,
# unless it sits under a system root that says nothing about the user.
_ABS_PATH = re.compile(r"(?<![\w.~/:\\…-])(?:/[\w.@+-]+){2,}/?")
_SYSTEM_ROOTS = ("/usr/", "/opt/", "/bin/", "/sbin/", "/etc/", "/dev/", "/proc/", "/tmp/",
                 "/var/", "/private/", "/System/", "/Library/", "/Applications/", "/nix/")

SECRET = "<secret>"
EMAIL = "<email>"


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(SECRET, text)
    for pattern in _LABELLED:
        text = pattern.sub(lambda m: m.group(1) + SECRET, text)
    text = _URL_CREDENTIALS.sub(lambda m: m.group(1) + SECRET + "@", text)
    text = _EMAIL.sub(EMAIL, text)
    text = _HOME.sub("~", text)
    return _ABS_PATH.sub(_shorten_path, text)


def _shorten_path(m: re.Match) -> str:
    path = m.group(0)
    if path.startswith(_SYSTEM_ROOTS):
        return path
    return "…/" + path.rstrip("/").rsplit("/", 1)[-1]
