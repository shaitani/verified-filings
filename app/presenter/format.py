"""Display strings for figures. The exact value always travels beside them."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.schemas.answer_view import UnitKind


class PresentationError(Exception):
    """A ``ResultSet`` the Presenter will not render. Our fault, never the asker's."""


#: Every unit the load step lets in (``app.db.models.ALLOWED_UNITS``). An unknown
#: one is refused: guessing a format is how a ratio ends up shown as money.
UNIT_KINDS: dict[str, UnitKind] = {
    "USD": "money",
    "EUR": "money",
    "pure": "ratio",
    "Rate": "ratio",
    "USD/shares": "per_share",
    "shares": "count",
}
_SYMBOL = {"USD": "$", "EUR": "€", "USD/shares": "$"}

#: Arithmetic over time: a positive result is shown with its sign.
_SIGNED = {"change", "growth", "cagr"}

_SCALES = ((Decimal(10) ** 12, "T"), (Decimal(10) ** 9, "B"), (Decimal(10) ** 6, "M"))

MISSING = "—"  # a derived row with no base: growth from zero, the first of a series


def unit_kind(unit: str) -> UnitKind:
    try:
        return UNIT_KINDS[unit]
    except KeyError:
        raise PresentationError(f"no display rule for unit {unit!r}") from None


def display(value: Decimal | None, unit: str, derivation: str | None) -> str:
    if value is None:
        return MISSING
    kind = unit_kind(unit)
    magnitude = abs(value)
    if kind == "ratio":
        percent = _round(magnitude * 100, 1)
        text = percent + (" pp" if derivation == "change" else "%")  # a ratio's change is points
    elif kind == "per_share":
        text = _SYMBOL[unit] + _round(magnitude, 2)
    elif kind == "money":
        text = _SYMBOL[unit] + _scaled(magnitude)
    else:
        text = _scaled(magnitude) + " shares"
    if value < 0:
        return "-" + text
    if value > 0 and derivation in _SIGNED:
        return "+" + text  # a rise says so; a filed figure does not
    return text


def period_label(fiscal_year: int, fiscal_period: str) -> str:
    return f"FY{fiscal_year}" if fiscal_period == "FY" else f"{fiscal_period} FY{fiscal_year}"


def _scaled(magnitude: Decimal) -> str:
    for size, suffix in _SCALES:
        if magnitude >= size:
            return _round(magnitude / size, 2) + suffix  # "$391.04B"
    return f"{magnitude.quantize(Decimal(1), ROUND_HALF_UP):,}"  # under a million: in full


def _round(value: Decimal, places: int) -> str:
    return f"{value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP):,}"
