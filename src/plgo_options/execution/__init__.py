"""Deribit execution bot: the trading-logic side of the logic/connector split.

The logic decides WHAT to trade and HOW to work it; the connector (Lucas)
only talks to Deribit. The two meet at the contract in ``models`` and
``connector`` ("Deribit Bot: Trading Logic & Handover", interface v1):

    logic --OrderIntent--> connector --orders--> Deribit
    logic <--OrderState / BookTop / Position / AccountState-- connector

Modules:
    models     The v1 messages: OrderIntent, OrderState, Fill, BookTop, Position, AccountState
    connector  The Connector protocol Lucas implements against Deribit
    paper      PaperConnector: fills against live (or stubbed) books, no account needed
    algo       work_order: walks one parent order from passive to aggressive, never past its limit
    hedge      Phase 1: a delta_hedger.RehedgeDecision -> an ExecutionRequest on the perp

Nothing here decides a view or a size: every ExecutionRequest comes from a
proposal the mandate gate (agents/gate.py) already approved. Wire the kill
switch in as ``halted=agents.store.kill_switch_on``.
"""
