# CART3R Solana Sniper — Paper Trading Starter

A Python/Streamlit **paper-trading-only** Solana new-pool scanner. It does not connect a wallet, hold keys, sign transactions, or submit swaps.

## What it does
- Discovers newly created Solana pools through GeckoTerminal's public API.
- Filters by pool age, liquidity, 5-minute volume, price availability, and sell imbalance.
- Requests a RugCheck token report before any paper entry.
- Fails closed: if RugCheck is unavailable, the token is rejected.
- Simulates entries/exits with configurable slippage and a 1% fee assumption.
- Tracks virtual cash, positions, P&L, and trade history in SQLite.
- Provides a Streamlit dashboard and adjustable risk controls.

## Default settings
- Virtual balance: $100
- Paper entry: $5
- Max open positions: 2
- Max entries/day: 5
- Max pool age: 30 minutes
- Minimum liquidity: $15,000
- Minimum 5-minute volume: $5,000
- Stop-loss: -20%
- Take-profit: +40%
- Daily realized-loss cap: $10

These are conservative test parameters, not evidence of profitability. Fresh-token data can be missing, delayed, or manipulated. A clean risk report does not guarantee a token is safe.

## Install (Windows PowerShell)
1. Install Python 3.11 or newer.
2. Extract this ZIP and open PowerShell in the project folder.
3. Create and activate a virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

4. Install dependencies:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

5. Start the dashboard:

```powershell
streamlit run app.py
```

6. Open the local URL printed by Streamlit (usually http://localhost:8501).
7. Click **Scan new pools**. The public GeckoTerminal API is rate-limited; the scanner deliberately pauses between RugCheck requests.

## Important limitations
- This is a paper-trading prototype, not a production trading system.
- Stop-loss/take-profit checks run when you press **Refresh prices / exits**; they are not a continuously running execution service.
- Public API rate limits and schema changes can interrupt discovery.
- GeckoTerminal new-pools are pools, not necessarily brand-new token mints.
- RugCheck is one risk signal, not an audit or guarantee. The code blocks entries when the report cannot be fetched.
- The scanner's 5-minute volume and pool fields depend on what GeckoTerminal returns for a particular pool.
- The 1% simulated fee and slippage assumptions are placeholders; actual venues and routes differ.
- Do not use this code to handle seed phrases or private keys. No live mode is included.

## Data sources
- GeckoTerminal API: https://api.geckoterminal.com/api/v2
- RugCheck API: https://api.rugcheck.xyz/v1

## Reset paper data
Stop Streamlit, then delete `sniper.db`. The next launch creates a fresh $100 virtual account.
