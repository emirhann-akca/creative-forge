"""studio-ui (ui.css, ui.js) is the design system shared with Studio Pulse, byte-identical in
both repos. A change bumps the version line and these hashes in both."""

from __future__ import annotations

import hashlib
from pathlib import Path

import creative_forge

STATIC = Path(creative_forge.__file__).parent / "ui" / "static"
UI_VERSION = "studio-ui 1.1.0"
UI_CSS_SHA256 = "4c8bba0bc59ad9396317b839a0c07ffe28d83b223c4b3bb4b045f6a0119e612c"
UI_JS_SHA256 = "45324136adb91282a87b43484761936ae1c16fbc5f3e84d4fe9464d914a5ec93"


def test_shared_design_system_files_are_pinned():
    css = (STATIC / "ui.css").read_bytes()
    js = (STATIC / "ui.js").read_bytes()
    assert css.decode("utf-8").startswith(f"/* {UI_VERSION} ")
    assert js.decode("utf-8").startswith(f"/* {UI_VERSION} ")
    assert hashlib.sha256(css).hexdigest() == UI_CSS_SHA256
    assert hashlib.sha256(js).hexdigest() == UI_JS_SHA256
    # Studio Pulse inlines them into <style> / <script>: no closing tag may appear inside
    assert b"</style" not in css.lower() and b"</script" not in js.lower()
