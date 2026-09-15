import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from panel.config import LineConfig  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name: str):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def sailings_payload():
    """Real archived /api/vacations/sailings/JOY3MIANASNPIMIA response."""
    return load_fixture("ncl_sailings_JOY3MIANASNPIMIA.json")


@pytest.fixture(scope="session")
def search_payload():
    """Real archived /api/v2/vacations/search response."""
    return load_fixture("ncl_search_page.json")


@pytest.fixture(scope="session")
def near_term_payload():
    return load_fixture("ncl_search_near_term.json")


@pytest.fixture
def ncl_line_cfg():
    """Mirrors config/panel.yaml so tests fail if the shipped mapping changes."""
    return LineConfig(
        key="ncl",
        line="Norwegian Cruise Line",
        brand="NCL",
        enabled=True,
        base_url="https://www.ncl.com",
        cabin_map={
            "STUDIO": "inside",
            "INSIDE": "inside",
            "OCEANVIEW": "oceanview",
            "BALCONY": "balcony",
            "MINISUITE": "balcony",
            "SUITE": "suite",
            "HAVEN": "suite",
        },
        region_map={
            "CARIBBEAN": "Caribbean",
            "BAHAMAS": "Caribbean",
            "MEDITERRANEAN": "Southern Europe",
            "GREEK_ISLES": "Southern Europe",
            "NORTHERN_EUROPE": "Northern Europe",
            "BERMUDA": "Bermuda",
            "ALASKA": "Alaska",
        },
        regions=["Caribbean", "Southern Europe", "Northern Europe",
                 "Bermuda", "Alaska"],
    )
