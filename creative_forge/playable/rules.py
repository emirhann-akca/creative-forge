"""Per-network playable ad rules, each with the page it comes from and the date that page was read.

The rules conflict between networks (MRAID 2.0 at AppLovin, MRAID 3.0 injected at Unity, MRAID forbidden at TikTok;
a single HTML file here, a zip there), so one HTML file cannot pass everywhere; ``forge playable pack`` builds one
per network.

Where a network's page was not readable (Liftoff/Vungle and Moloco answered 403 to the research) or does not state a
rule (Mintegral's CTA call), the rule is reported as ``uncertain``, never as a failure. A rule whose wording leaves
room ("no self-closing tags": void elements only?) is also ``uncertain`` when it fires.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHECKED = "2026-11-11"
MB = 1_000_000
MIB = 1_048_576

SOURCES = {
    "applovin": "https://support.applovin.com/en/app-discovery/creatives/best-practices-and-guidelines",
    "unity": "https://docs.unity.com/en-us/user-acquisition/creatives/specifications/playable",
    "ironsource": "https://docs.unity.com/en-us/grow/is-ads/ironsource-ads-sunset",
    "meta": "https://developers.facebook.com/docs/app-ads/formats/playable-ad",
    "google": "https://support.google.com/google-ads/answer/9981650",
    "google-troubleshooting": "https://support.google.com/google-ads/answer/12771973",
    "tiktok": "https://ads.tiktok.com/help/article?aid=12099",
    "tiktok-pangle": "https://ads.tiktok.com/help/article/how-to-create-tiktok-pangle-playable-ads",
    "mintegral": "https://helpcenter.mintegral.com/en/docs/asset-specs",
    "mraid3": "https://iabtechlab.com/wp-content/uploads/2018/06/MRAID_3.0_FINAL_June_2018.pdf",
    "none": "",
}

PASS, FAIL, WARN, UNCERTAIN, SKIP, INFO = "pass", "fail", "warn", "uncertain", "skip", "info"
ORDER = {PASS: 0, INFO: 0, SKIP: 1, WARN: 2, UNCERTAIN: 3, FAIL: 4}


@dataclass(frozen=True)
class Finding:
    rule: str
    status: str
    message: str
    source: str = ""
    checked: str = CHECKED

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "status": self.status,
            "message": self.message,
            "source": self.source,
            "checked": self.checked,
        }


@dataclass(frozen=True)
class Network:
    name: str
    label: str
    packaging: str  # "html" (one file), "zip", "html_or_zip"
    source: str  # key into SOURCES of the main spec page
    verified: bool = True  # False: the spec page could not be read; network-specific rules are "uncertain"
    html_max: int | None = None  # bytes, decimal MB as the page states it
    zip_max: int | None = None
    zip_max_files: int | None = None
    zip_soft_max: int | None = None  # a second official figure (Google's troubleshooting page says 5.2 MB)
    file_types: tuple[str, ...] | None = None  # allowed extensions inside a zip
    mraid: str = "none"  # "required", "injected", "forbidden", "none" (not part of the spec), "unverified"
    mraid_version: str | None = None
    cta: tuple[str, ...] = ()  # the exit/CTA call the network expects
    sdk_script: str | None = None  # a script the playable must load (Google exitapi.js, TikTok playable-sdk.js)
    allowed_hosts: tuple[str, ...] = ()  # external hosts a playable may load from
    external: str = WARN  # status of an external request
    first_tap_store: str = WARN  # status when the first tap opens the store
    audio_before_tap: str = INFO
    landscape: str = WARN  # status when the playable breaks in landscape
    render_s: float = 5.0  # first non-blank frame within this many seconds
    render_status: str = FAIL
    needs_store_url: bool = False  # the CTA call takes the store URL (mraid.open(url))
    notes: tuple[str, ...] = field(default_factory=tuple)


NETWORKS: dict[str, Network] = {
    "applovin": Network(
        "applovin",
        "AppLovin",
        "html",
        "applovin",
        html_max=5 * MB,
        mraid="required",
        mraid_version="2.0",
        cta=("mraid.open",),
        external=FAIL,
        first_tap_store=FAIL,
        audio_before_tap=WARN,
        landscape=FAIL,
        needs_store_url=True,
        notes=("Timer starts after the first interaction; WebGL needs a context-loss fallback.",),
    ),
    "unity": Network(
        "unity",
        "Unity Ads",
        "html",
        "unity",
        html_max=5 * MB,
        mraid="injected",
        mraid_version="3.0",
        cta=("mraid.open",),
        external=FAIL,
        first_tap_store=FAIL,
        landscape=WARN,
        needs_store_url=True,
        notes=("A single minified index.html with every asset inlined; start after ready and viewable.",),
    ),
    "meta": Network(
        "meta",
        "Meta",
        "html_or_zip",
        "meta",
        html_max=2 * MB,
        zip_max=5 * MB,
        zip_max_files=100,
        cta=("FbPlayableAd.onCTAClick",),
        notes=("Missing CTA call means rejection; no JavaScript redirects.",),
    ),
    "google": Network(
        "google",
        "Google Ads (HTML5 app ads)",
        "zip",
        "google",
        zip_max=5 * MB,
        zip_soft_max=5_200_000,
        zip_max_files=512,
        file_types=(".css", ".gif", ".htm", ".html", ".jpeg", ".jpg", ".js", ".png", ".svg"),
        cta=("ExitApi.exit",),
        sdk_script="exitapi.js",
        allowed_hosts=(
            "tpc.googlesyndication.com",
            "fonts.googleapis.com",
            "fonts.gstatic.com",
            "ajax.googleapis.com",
            "s0.2mdn.net",
        ),
        external=FAIL,
        audio_before_tap=FAIL,
        notes=(
            'Needs <meta name="ad.orientation">; external references only to Google Fonts and Google-hosted '
            "jQuery, GreenSock and CreateJS.",
        ),
    ),
    "tiktok": Network(
        "tiktok",
        "TikTok (Pangle)",
        "zip",
        "tiktok",
        zip_max=5 * MB,
        mraid="forbidden",
        cta=("openAppStore",),
        sdk_script="playable-sdk.js",
        external=FAIL,
        render_s=2.0,
        render_status=WARN,
        notes=(
            "index.html + config.json (playable_orientation 0/1/2); a companion video or image is uploaded "
            "separately; the CTA must stay visible.",
        ),
    ),
    "mintegral": Network(
        "mintegral",
        "Mintegral",
        "html",
        "mintegral",
        html_max=5 * MB,
        mraid="unverified",
        cta=("install", "mraid.open"),
        landscape=UNCERTAIN,
        notes=(
            "A main image and an app icon are uploaded with it. The playable JS API (install/game-end calls) "
            "was not verified.",
        ),
    ),
    "liftoff": Network("liftoff", "Liftoff (Vungle)", "html_or_zip", "none", verified=False, mraid="unverified"),
    "moloco": Network("moloco", "Moloco", "html_or_zip", "none", verified=False, mraid="unverified"),
}

SUNSET = {
    "ironsource": (
        "ironSource Ads direct demand ended on 2026-04-30 and advertisers were moved to Unity Ads; check against "
        "--network unity instead",
        "ironsource",
    )
}

CTA_LABELS = {
    "mraid.open": "mraid.open(url)",
    "FbPlayableAd.onCTAClick": "FbPlayableAd.onCTAClick()",
    "ExitApi.exit": "ExitApi.exit()",
    "openAppStore": "window.openAppStore()",
    "install": "window.install()",
    "postMessage:download": "parent.postMessage('download')",
    "window.open": "window.open(url)",
}


def source_of(net: Network) -> str:
    return SOURCES.get(net.source, "")


def worst(findings: list[Finding]) -> str:
    return max((f.status for f in findings), key=lambda s: ORDER.get(s, 0), default=PASS)
