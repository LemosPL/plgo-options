# PLGO Options

ETH options pricing and portfolio management web app powered by Deribit market data.

## Features

- **Options Pricer** — Price ETH options using Black-Scholes with live vol surface from Deribit
- **Portfolio P&L** — Track positions with real-time MTM, Greeks, payoff profiles, and scenario matrix
- **Roll Analysis** — Analyze roll strategies with cost decomposition, baseline vs scenario comparison, and Excel export

## Setup

Requires Python 3.11+.

```bash
# Clone the repo
git clone https://github.com/LemosPL/plgo-options.git
cd plgo-options

# Create a virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate      # Linux/Mac
.venv\Scripts\activate         # Windows

pip install -e .
```

## Run

```bash
PYTHONPATH=src python -m uvicorn plgo_options.web.app:app --reload
```

Then open http://127.0.0.1:8000 in your browser.
