"""Store URLs: a campaign is refused unless its URL is a real store listing format and not a placeholder.

The playable ads shipped with ``id=DOLDUR`` ("fill in") in their store link, which on a live network is an install
button that opens a dead page. A paid campaign with the same mistake spends money on clicks that cannot install, so
the check is strict and runs before anything is planned:

* the URL is ``https`` on ``play.google.com/store/apps/details?id=<package>`` or ``apps.apple.com/.../id<digits>``;
* the package name is a valid Android application id, the App Store id is 6-12 digits;
* no part of it contains a placeholder word (DOLDUR, TODO, TBD, FIXME, example, placeholder, changeme, your-app, xxx)
  and the App Store id is not a run of one repeated digit.

``verify_online`` additionally fetches the listing (an unauthenticated GET of a public page, never part of the tests)
for a person who wants to know the app is published.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

import requests

PLACEHOLDERS = (
    "doldur",
    "todo",
    "tbd",
    "fixme",
    "example",
    "placeholder",
    "changeme",
    "change-me",
    "change_me",
    "your-app",
    "your_app",
    "yourapp",
    "xxx",
    "lorem",
    "dummy",
)
ANDROID_PACKAGE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")
IOS_PATH = re.compile(r"^/(?:[a-z]{2}/)?app/(?:[^/]+/)?id(\d{6,12})/?$")


class StoreUrlError(ValueError):
    """The URL is not a store listing a campaign may point at."""


@dataclass(frozen=True)
class StoreListing:
    platform: str  # "android" or "ios"
    app_id: str  # package name, or the numeric App Store id
    url: str

    @property
    def google_store(self) -> str:
        """Google Ads ``AppCampaignAppStore``."""
        return "GOOGLE_APP_STORE" if self.platform == "android" else "APPLE_APP_STORE"


def placeholder_in(text: str) -> str | None:
    low = urllib.parse.unquote(text).lower()
    for word in PLACEHOLDERS:
        if word in low:
            return word
    return None


def parse_store_url(url: str) -> StoreListing:
    url = (url or "").strip()
    if not url:
        raise StoreUrlError("no store URL")
    word = placeholder_in(url)
    if word:
        raise StoreUrlError(f"{url!r} contains the placeholder {word!r}; put the published store listing here")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise StoreUrlError(f"{url!r}: a store URL must be https")
    host = (parts.hostname or "").lower()
    if host == "play.google.com":
        if parts.path.rstrip("/") != "/store/apps/details":
            raise StoreUrlError(f"{url!r}: a Google Play listing is /store/apps/details?id=<package>")
        ids = urllib.parse.parse_qs(parts.query).get("id", [])
        if len(ids) != 1 or not ANDROID_PACKAGE.fullmatch(ids[0]):
            raise StoreUrlError(f"{url!r}: id= must be one Android package name (com.studio.game)")
        return StoreListing("android", ids[0], url)
    if host in ("apps.apple.com", "itunes.apple.com"):
        m = IOS_PATH.fullmatch(parts.path)
        if not m:
            raise StoreUrlError(f"{url!r}: an App Store listing is apps.apple.com/<country>/app/<name>/id<digits>")
        app_id = m.group(1)
        if len(set(app_id)) == 1:
            raise StoreUrlError(f"{url!r}: id{app_id} is a run of one digit, not an App Store id")
        return StoreListing("ios", app_id, url)
    raise StoreUrlError(f"{url!r}: not a Google Play or App Store listing")


def verify_online(listing: StoreListing, session: requests.Session | None = None, timeout_s: float = 15.0) -> str:
    """Fetch the public listing page; return "" when it answers 200, otherwise the problem. Opt-in only."""
    s = session or requests.Session()
    try:
        r = s.get(listing.url, timeout=timeout_s, allow_redirects=True, headers={"User-Agent": "creative-forge"})
    except requests.RequestException as e:
        return f"{type(e).__name__} fetching {urllib.parse.urlsplit(listing.url).hostname}"
    if r.status_code != 200:
        return f"the store answered HTTP {r.status_code} for {listing.url}"
    return ""
