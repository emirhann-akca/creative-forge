"""Every example ad name written in naming.py's docstring must parse, so the docs cannot drift from the code."""

import re

from creative_forge import naming


def test_docstring_example_ad_names_parse():
    examples = re.findall(r"cf1\.[a-z0-9_.\-]+\.[0-9a-f]{10}\b", naming.__doc__ or "")
    assert examples, "naming.py should show at least one concrete ad name"
    for name in examples:
        ids = naming.parse(name)
        assert ids.ad_name == name
