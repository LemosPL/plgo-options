
from pathlib import Path

import openpyxl


def read_eth_trades() -> list[dict]:
    """Read trades from the 'Trades' tab of the ETH dashboard Excel file."""
    file_path = Path(__file__).resolve().parent.parent / "data" / "ETH - Dashboard Risk+PnL Improvement Proposal.xlsx"

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb["Trades"]

    rows = ws.iter_rows(values_only=True)

    # Scan for the header row that starts with "Counterparty"
    headers = None
    for row in rows:
        if row and str(row[0]).strip().lower() == "counterparty":
            headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(row)]
            break

    if headers is None:
        wb.close()
        raise ValueError("Could not find header row starting with 'Counterparty' in the 'Trades' sheet")

    trades = []
    for row in rows:
        if all(cell is None for cell in row):
            continue
        trades.append(dict(zip(headers, row)))

    wb.close()
    return trades


if __name__ == "__main__":
    trades = read_eth_trades()
    print(f"Loaded {len(trades)} trades\n")
    for trade in trades[:5]:
        print(trade)
    if len(trades) > 5:
        print(f"\n... and {len(trades) - 5} more trades")