from pathlib import Path
import pandas as pd

xlsx_path = Path("/Users/tintin/PycharmProjects/plgo-options/data/positions/PLGO_Trades_2026-05-26.xlsx")
csv_path = xlsx_path.with_suffix(".csv")

df = pd.read_excel(xlsx_path)
df.to_csv(csv_path, index=False)

print(f"Created CSV: {csv_path}")