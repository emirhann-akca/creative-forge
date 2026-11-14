"""The API versions the campaign adapters target and the pages their request shapes come from (read 2026-11-14).

Nothing here was called against a live account. The shapes were taken from these pages and are exercised against the
local mock network (``mock_server.py``) only.
"""

from __future__ import annotations

READ_ON = "2026-11-14"

# Meta Marketing API: v27.0 was introduced on 2026-11-04 (Graph API changelog); v26.0 on 2026-07-29.
META_VERSION = "v27.0"
META_DOCS = {
    "changelog": "https://developers.facebook.com/docs/graph-api/changelog",
    "campaign": "https://developers.facebook.com/docs/marketing-api/reference/ad-campaign-group",
    "advantage_plus": "https://developers.facebook.com/docs/marketing-api/advantage-campaigns",
    "app_campaigns": "https://developers.facebook.com/docs/app-ads/advantage-app-campaigns",
    "sandbox": "https://developers.facebook.com/documentation/development/create-an-app/marketing-api-use-cases",
}

# TikTok API for Business: every endpoint lives under /open_api/v1.3/ (official SDK, tiktok/tiktok-business-api-sdk).
TIKTOK_VERSION = "v1.3"
TIKTOK_DOCS = {
    "portal": "https://business-api.tiktok.com/portal/docs",
    "sdk": "https://github.com/tiktok/tiktok-business-api-sdk",
    "campaign_create": "https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/CampaignCreateBody.md",
    "adgroup_create": "https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/AdgroupCreateBody.md",
    "ad_create": "https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/AdcreateCreatives.md",
    "file_upload": "https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/FileApi.md",
}

# Google Ads API: v26 was released on 2026-10-14; v26.1 (2026-10-28) and v26.2 (2026-11-11) are minor releases without
# breaking changes. Requests use the v26 major path.
GOOGLE_ADS_VERSION = "v26"
GOOGLE_ADS_DOCS = {
    "release_notes": "https://developers.google.com/google-ads/api/docs/release-notes",
    "app_campaign": "https://developers.google.com/google-ads/api/docs/app-campaigns/create-campaign",
    "campaign": "https://developers.google.com/google-ads/api/reference/rpc/v26/Campaign",
    "video_upload": "https://developers.google.com/google-ads/api/docs/assets/upload-videos",
    "test_accounts": "https://developers.google.com/google-ads/api/docs/best-practices/test-accounts",
}

# Networks with a documented campaign API whose create call is NOT documented to start paused: manual path only.
MANUAL_DOCS = {
    "applovin": "https://support.applovin.com/en/growth/promoting-your-apps/api/axon-campaign-management-api",
    "unity": "https://docs.unity.com/en-us/user-acquisition/management/api-campaign-management",
    "mintegral": "https://helpcenter.mintegral.com/en/docs/create-ad-unit-api",
}

VERSIONS = {"meta": META_VERSION, "tiktok": TIKTOK_VERSION, "google": GOOGLE_ADS_VERSION}
