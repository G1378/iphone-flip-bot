"""The complete iPhone line-up, in logical order, with the storage sizes and colourways each model came in.

Order: oldest series first; inside a series the tiers go up (e / mini, base, Plus / Air, Pro, Pro Max).
`sort_order` = series number * 100 + position in the series, so the bot can show the list oldest-first
(`/config phone-models`) or newest-first (dropdowns, autocomplete) without extra bookkeeping.

These feed the dropdowns when a phone or donor is bought. Colour names are Apple's own and are also what
frames / back glass are matched on, so keep them consistent. Storage uses the same "128GB" / "1TB" format the
phones table has always held. Models and options can be added or changed with `/config phone-models`; the
seeding below never overwrites an option list someone has already set.

Sources: Apple's tech specs and newsroom releases (iPhone 17e, March 2026; iPhone 18 Pro / Pro Max and iPhone
Duo, 9 September 2026). iPhone 18 / 18e / Air 2 are not released yet, so are not listed."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

RED = "(PRODUCT)RED"


@dataclass(frozen=True)
class CatalogueModel:
    name: str
    series: str
    year: int
    storage: tuple[str, ...]
    colours: tuple[str, ...]
    sort_order: int


def _m(name: str, storage: str, colours: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return name, tuple(storage.split(",")), tuple(colours.split("|"))


# (series, year, [models in tier order])
_SERIES: list[tuple[str, int, list]] = [
    ("iPhone (original)", 2007, [
        _m("iPhone (1st generation)", "4GB,8GB,16GB", "Black"),
    ]),
    ("iPhone 3 series", 2008, [
        _m("iPhone 3G", "8GB,16GB", "Black|White"),
        _m("iPhone 3GS", "8GB,16GB,32GB", "Black|White"),
    ]),
    ("iPhone 4 series", 2010, [
        _m("iPhone 4", "8GB,16GB,32GB", "Black|White"),
        _m("iPhone 4S", "8GB,16GB,32GB,64GB", "Black|White"),
    ]),
    ("iPhone 5 series", 2012, [
        _m("iPhone 5", "16GB,32GB,64GB", "Black & Slate|White & Silver"),
        _m("iPhone 5c", "8GB,16GB,32GB", "Blue|Green|Pink|Yellow|White"),
        _m("iPhone 5s", "16GB,32GB,64GB", "Space Grey|Silver|Gold"),
    ]),
    ("iPhone 6 series", 2014, [
        _m("iPhone 6", "16GB,32GB,64GB,128GB", "Space Grey|Silver|Gold"),
        _m("iPhone 6 Plus", "16GB,64GB,128GB", "Space Grey|Silver|Gold"),
    ]),
    ("iPhone 6s series", 2015, [
        _m("iPhone 6s", "16GB,32GB,64GB,128GB", "Space Grey|Silver|Gold|Rose Gold"),
        _m("iPhone 6s Plus", "16GB,32GB,64GB,128GB", "Space Grey|Silver|Gold|Rose Gold"),
    ]),
    ("iPhone SE", 2016, [
        _m("iPhone SE (1st generation)", "16GB,32GB,64GB,128GB", "Space Grey|Silver|Gold|Rose Gold"),
        _m("iPhone SE (2nd generation)", "64GB,128GB,256GB", f"Black|White|{RED}"),
        _m("iPhone SE (3rd generation)", "64GB,128GB,256GB", f"Midnight|Starlight|{RED}"),
    ]),
    ("iPhone 7 series", 2016, [
        _m("iPhone 7", "32GB,128GB,256GB", f"Black|Jet Black|Silver|Gold|Rose Gold|{RED}"),
        _m("iPhone 7 Plus", "32GB,128GB,256GB", f"Black|Jet Black|Silver|Gold|Rose Gold|{RED}"),
    ]),
    ("iPhone 8 series", 2017, [
        _m("iPhone 8", "64GB,128GB,256GB", f"Space Grey|Silver|Gold|{RED}"),
        _m("iPhone 8 Plus", "64GB,128GB,256GB", f"Space Grey|Silver|Gold|{RED}"),
    ]),
    ("iPhone X series", 2017, [
        _m("iPhone X", "64GB,256GB", "Space Grey|Silver"),
        _m("iPhone XR", "64GB,128GB,256GB", f"Black|White|Blue|Yellow|Coral|{RED}"),
        _m("iPhone XS", "64GB,256GB,512GB", "Space Grey|Silver|Gold"),
        _m("iPhone XS Max", "64GB,256GB,512GB", "Space Grey|Silver|Gold"),
    ]),
    ("iPhone 11 series", 2019, [
        _m("iPhone 11", "64GB,128GB,256GB", f"Black|White|Green|Yellow|Purple|{RED}"),
        _m("iPhone 11 Pro", "64GB,256GB,512GB", "Space Grey|Silver|Gold|Midnight Green"),
        _m("iPhone 11 Pro Max", "64GB,256GB,512GB", "Space Grey|Silver|Gold|Midnight Green"),
    ]),
    ("iPhone 12 series", 2020, [
        _m("iPhone 12 mini", "64GB,128GB,256GB", f"Black|White|{RED}|Green|Blue|Purple"),
        _m("iPhone 12", "64GB,128GB,256GB", f"Black|White|{RED}|Green|Blue|Purple"),
        _m("iPhone 12 Pro", "128GB,256GB,512GB", "Graphite|Silver|Gold|Pacific Blue"),
        _m("iPhone 12 Pro Max", "128GB,256GB,512GB", "Graphite|Silver|Gold|Pacific Blue"),
    ]),
    ("iPhone 13 series", 2021, [
        _m("iPhone 13 mini", "128GB,256GB,512GB", f"Pink|Blue|Midnight|Starlight|{RED}|Green"),
        _m("iPhone 13", "128GB,256GB,512GB", f"Pink|Blue|Midnight|Starlight|{RED}|Green"),
        _m("iPhone 13 Pro", "128GB,256GB,512GB,1TB", "Graphite|Gold|Silver|Sierra Blue|Alpine Green"),
        _m("iPhone 13 Pro Max", "128GB,256GB,512GB,1TB", "Graphite|Gold|Silver|Sierra Blue|Alpine Green"),
    ]),
    ("iPhone 14 series", 2022, [
        _m("iPhone 14", "128GB,256GB,512GB", f"Blue|Purple|Midnight|Starlight|{RED}|Yellow"),
        _m("iPhone 14 Plus", "128GB,256GB,512GB", f"Blue|Purple|Midnight|Starlight|{RED}|Yellow"),
        _m("iPhone 14 Pro", "128GB,256GB,512GB,1TB", "Space Black|Silver|Gold|Deep Purple"),
        _m("iPhone 14 Pro Max", "128GB,256GB,512GB,1TB", "Space Black|Silver|Gold|Deep Purple"),
    ]),
    ("iPhone 15 series", 2023, [
        _m("iPhone 15", "128GB,256GB,512GB", "Black|Blue|Green|Yellow|Pink"),
        _m("iPhone 15 Plus", "128GB,256GB,512GB", "Black|Blue|Green|Yellow|Pink"),
        _m("iPhone 15 Pro", "128GB,256GB,512GB,1TB", "Black Titanium|White Titanium|Blue Titanium|Natural Titanium"),
        _m("iPhone 15 Pro Max", "256GB,512GB,1TB", "Black Titanium|White Titanium|Blue Titanium|Natural Titanium"),
    ]),
    ("iPhone 16 series", 2024, [
        _m("iPhone 16e", "128GB,256GB,512GB", "Black|White"),
        _m("iPhone 16", "128GB,256GB,512GB", "Black|White|Pink|Teal|Ultramarine"),
        _m("iPhone 16 Plus", "128GB,256GB,512GB", "Black|White|Pink|Teal|Ultramarine"),
        _m("iPhone 16 Pro", "128GB,256GB,512GB,1TB", "Black Titanium|White Titanium|Natural Titanium|Desert Titanium"),
        _m("iPhone 16 Pro Max", "256GB,512GB,1TB", "Black Titanium|White Titanium|Natural Titanium|Desert Titanium"),
    ]),
    ("iPhone 17 series", 2025, [
        _m("iPhone 17e", "256GB,512GB", "Black|White|Soft Pink"),
        _m("iPhone 17", "256GB,512GB", "Black|White|Mist Blue|Sage|Lavender"),
        _m("iPhone Air", "256GB,512GB,1TB", "Space Black|Cloud White|Light Gold|Sky Blue"),
        _m("iPhone 17 Pro", "256GB,512GB,1TB", "Silver|Cosmic Orange|Deep Blue"),
        _m("iPhone 17 Pro Max", "256GB,512GB,1TB,2TB", "Silver|Cosmic Orange|Deep Blue"),
    ]),
    ("iPhone 18 series", 2026, [
        _m("iPhone 18 Pro", "256GB,512GB,1TB,2TB", "Black|Silver|Glacier|Burgundy"),
        _m("iPhone 18 Pro Max", "256GB,512GB,1TB,2TB", "Black|Silver|Glacier|Burgundy"),
        _m("iPhone Duo", "256GB,512GB,1TB,2TB", "Star White|Night Sky"),
    ]),
]

OTHER_SERIES = "Other models"


def _build() -> list[CatalogueModel]:
    out = []
    for s_idx, (series, year, models) in enumerate(_SERIES):
        for pos, (name, storage, colours) in enumerate(models):
            out.append(CatalogueModel(name, series, year, storage, colours, s_idx * 100 + pos))
    return out


CATALOGUE: list[CatalogueModel] = _build()
SERIES_ORDER: list[str] = [s for s, _, _ in _SERIES]
BY_NAME = {m.name: m for m in CATALOGUE}


def series_year(series: str) -> Optional[int]:
    return next((y for s, y, _ in _SERIES if s == series), None)
