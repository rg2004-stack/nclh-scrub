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


@pytest.fixture(scope="session")
def ccl_search_us():
    """Real archived /cruisesearch/api/search response (US/USD)."""
    return load_fixture("ccl_search_us.json")


@pytest.fixture(scope="session")
def ccl_page1():
    return load_fixture("ccl_search_page1.json")


@pytest.fixture(scope="session")
def ccl_page2():
    return load_fixture("ccl_search_page2.json")


@pytest.fixture
def ccl_line_cfg():
    """Mirrors the carnival block in config/panel.yaml so the tests fail if the
    shipped mapping changes underneath them."""
    return LineConfig(
        key="carnival",
        line="Carnival Cruise Line",
        brand="Carnival",
        enabled=True,
        base_url="https://www.carnival.com",
        cabin_map={"IS": "inside", "OS": "oceanview",
                   "OB": "balcony", "SU": "suite"},
        region_map={
            "ME": "Southern Europe", "GI": "Southern Europe",
            "CG": "Southern Europe", "IB": "Southern Europe",
            "EC": "Southern Europe",
            "EN": "Northern Europe", "ES": "Northern Europe",
            "BI": "Northern Europe",
            "CE": "Caribbean", "CW": "Caribbean", "BH": "Caribbean",
            "BM": "Bermuda", "GL": "Alaska",
        },
        # LON deliberately absent: it serves both Northern and Iberian routes.
        port_region_map={"BCN": "Southern Europe", "CIV": "Southern Europe",
                         "LIS": "Southern Europe"},
        regions=["Caribbean", "Southern Europe", "Northern Europe",
                 "Bermuda", "Alaska"],
        search_page_size=20,
    )
