"""Trade Management decay calendar: month-end checkpoints for value_path."""

from datetime import date

from plgo_options.web.routes.portfolio import _decay_calendar


def test_checkpoints_are_now_then_each_month_end():
    cal = _decay_calendar(date(2026, 10, 13), months=4)
    assert [c["label"] for c in cal] == ["now", "Oct 26", "Nov 26", "Dec 26", "Jan 27"]
    assert [c["days"] for c in cal] == [0, 18, 48, 79, 110]
    assert cal[3]["date"] == "2026-12-31"


def test_a_month_end_today_is_day_zero_and_the_year_rolls():
    cal = _decay_calendar(date(2026, 12, 31), months=2)
    assert [(c["label"], c["days"]) for c in cal] == [("now", 0), ("Dec 26", 0), ("Jan 27", 31)]
