"""The Connector protocol: what the logic needs from the Deribit side.

Lucas implements this against the Deribit API; ``paper.PaperConnector``
implements it against order books with no account. The algo only ever sees
this protocol, so the same code runs on paper, testnet and production.

Rules every implementation keeps:
    - ``submit`` is idempotent on ``intent_id``: a resend returns the existing order.
    - An exchange rejection comes back as ``OrderStatus.REJECTED`` with a reason,
      not as an exception. Exceptions mean the connector itself is broken.
    - ``cancel`` on a finished order returns its final state unchanged.
    - ``healthy`` is False whenever the connection, auth or rate limit is in doubt;
      the logic sends nothing while it is False.
"""

from __future__ import annotations

from typing import Protocol

from .models import AccountState, BookTop, OrderIntent, OrderState, Position


class Connector(Protocol):
    async def submit(self, intent: OrderIntent) -> OrderState: ...

    async def cancel(self, order_id: str) -> OrderState: ...

    async def order(self, order_id: str) -> OrderState: ...

    async def book(self, instrument: str) -> BookTop: ...

    async def positions(self) -> list[Position]: ...

    async def account(self) -> AccountState: ...

    async def healthy(self) -> bool: ...
