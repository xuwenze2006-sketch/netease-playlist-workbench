"""The same strict URL boundary is used for links and local QR contents."""

import re
from urllib.parse import urlsplit


_OFFICIAL_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*music\.163\.com")
# Observed in the supported official CLI's actual QR authorization response.
# Only the exact HTTPS short host is accepted; subdomains remain disallowed.
_OFFICIAL_SHORT_HOST = "163cn.tv"


def safe_authorization_url(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or not value.isascii()
            or "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        return ""
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        if (url.scheme != "https" or len(host) > 253
                or not (_OFFICIAL_HOST.fullmatch(host) or host == _OFFICIAL_SHORT_HOST)
                or url.username is not None or url.password is not None or url.port not in (None, 443)):
            return ""
    except ValueError:
        return ""
    return value
