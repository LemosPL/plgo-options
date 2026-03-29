from datetime import datetime

from plgo_options.optimization.option_smile import OptionSmile


smile = OptionSmile([
    {
        "expiry_code": "27JUN26",
        "expiry_date": "2026-06-27",
        "strikes": [2000, 2500, 3000, 3500],
        "ivs": [0.92, 0.84, 0.79, 0.81],
    },
    {
        "expiry_code": "25JUL26",
        "expiry_date": "2026-07-25",
        "strikes": [2000, 2500, 3000, 3500],
        "ivs": [0.90, 0.82, 0.78, 0.80],
    },
])

iv = smile.compute_vol(datetime(2026, 7, 10), 2800)
print(iv)