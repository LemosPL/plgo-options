"""FIL legs are marked at each counterparty's own flat mark vol, not the
ETH-smile-scaled surface (which put 11-day FIL wings at 260-440%)."""

from datetime import date

from plgo_options.pricing.cpty_pricing import mark_vol_pct
from plgo_options.pricing.options import bs_price
from plgo_options.pricing.vol_surface import VolSmile
from plgo_options.web.routes.portfolio import _leg_iv

# Flowdesk statement, 19 Oct 2026: (signed qty, strike, type, their price).
FLOWDESK_30OCT26 = [
    (-18_000_000, 0.45, "P", 0.0), (-8_500_000, 0.5, "C", 0.5208), (8_500_000, 0.5, "P", 0.0),
    (12_000_000, 0.77, "P", 0.0101), (6_000_000, 0.87, "P", 0.0297), (2_500_000, 1.24, "C", 0.0274),
    (7_500_000, 1.49, "C", 0.0054), (7_000_000, 1.75, "C", 0.0009), (-7_000_000, 1.75, "P", 0.7282),
    (-8_000_000, 2.5, "C", 0.0),
]


def test_flowdesk_fil_book_marks_close_to_their_statement():
    spot, today = 1.0184, date(2026, 10, 19)
    # A steep surface like the scaled-ETH one: the Flowdesk mark must ignore it.
    smiles = {"30OCT26": VolSmile([0.45, 1.0, 2.5], [440.0, 148.0, 340.0])}
    dates = {"30OCT26": date(2026, 10, 30)}
    theirs = ours = 0.0
    for q, k, opt, px in FLOWDESK_30OCT26:
        iv = _leg_iv("Flowdesk", "FIL", "2026-10-30", k, spot, smiles, dates, today)
        assert iv == 130.0
        theirs += q * px
        ours += q * bs_price(spot, k, 11 / 365.25, 0.0, iv / 100.0, opt)
    assert abs(ours - theirs) < 25_000, (ours, theirs)


def test_uncalibrated_counterparty_and_eth_keep_the_surface():
    smiles = {"30OCT26": VolSmile([0.5, 1.5], [200.0, 200.0])}
    dates = {"30OCT26": date(2026, 10, 30)}
    assert _leg_iv("Wave", "FIL", "2026-10-30", 1.0, 1.0, smiles, dates, date(2026, 10, 19)) == 200.0
    assert mark_vol_pct("Flowdesk", "ETH", 2500.0, 2500.0) is None


def test_keyrock_marks_at_the_mid_of_its_quote():
    assert mark_vol_pct("KeyRock", "FIL", 1.0, 1.1) == (116.0 + 163.0) / 2
    assert mark_vol_pct("keyrock", "FIL", 1.0, 2.0) == (116.0 + 185.0) / 2   # far wing
