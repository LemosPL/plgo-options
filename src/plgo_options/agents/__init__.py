"""Desk agents for the ETH and FIL options books.

These agents automate the timetable in "Options Portfolio — Strategy and
Execution" (Part B). They read the book, compute, write briefs and propose
trades. They never decide the view (A1), never change limits (A3), never roll
and never send an option order. Every proposal goes through ``gate.evaluate``,
which routes it to Lucas, to Chris, or rejects it with the rule it broke.

Modules:
    policy     Monday's settings: view, reference prices, rows, limits, v4 preset
    store      SQLite tables: policy, row fires, runs, proposals, kill switch
    rows       Where spot sits against the B1 rows, and what each row says to do
    gate       The mandate gate (A2-A5, B1-B3) as plain code
    optimizer  Optimizer v4 sweep: target profile + λ/κ/T+90/max-trades grid
    desk       The eight agents (row watcher ... monthly review)
    narrate    Claude writes the prose from finished numbers (optional)
"""
