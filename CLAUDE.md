# Kalshi Trading Framework

## Project Purpose

Python-based trading and analysis framework for Kalshi prediction markets.
Primary strategy: **market making** — exploiting spread and microstructure, not predicting outcomes.
Directional positions only where backtested edge is demonstrated.
Target markets: high-repeatability, data-dense (daily temperature highs, Fed rate decisions, etc.).

## Architecture

```
kalshi/
├── api/            # Kalshi REST + WebSocket clients
├── data/           # Fetching, storage, caching of market/order data
│   └── raw/        # Gitignored local data files
├── strategies/     # Strategy implementations (MM and directional)
│   └── market_maker/
├── backtest/       # Event-driven backtest engine + metrics
├── risk/           # Position limits, exposure tracking, Greeks
├── analysis/       # Market analysis, fair value estimation, stats
├── notebooks/      # Exploratory analysis (not production code)
├── scripts/        # One-off utilities
└── tests/
```

## Kalshi API

- **REST base URL:** `https://api.elections.kalshi.com/trade-api/v2`
- **WebSocket:** `wss://api.elections.kalshi.com/trade-api/ws/v2`
- **Auth:** RSA key-based (key ID + private key) or API token in header `Authorization: Bearer <token>`
- **Prices:** integers 0–99 representing cents / probability in percent
- **Balance/portfolio values:** returned in cents (e.g., 1537 = $15.37). Always divide by 100 for dollars.
- **Order types:** limit only (no market orders)
- **Sides:** `yes` / `no` — a `no` at price P is equivalent to `yes` at price (100-P)
- **Settlement:** binary, $1 per contract face value
- **Fee structure:** maker/taker, check current fee schedule before assuming
- **Documentation Landing Page** https://docs.kalshi.com/welcome
Key endpoints:
```
GET  /markets                         # list/search markets
GET  /markets/{ticker}                # single market detail
GET  /markets/{ticker}/orderbook      # L2 orderbook
GET  /markets/{ticker}/trades         # recent trades
POST /portfolio/orders                # submit order
GET  /portfolio/orders                # open orders
DELETE /portfolio/orders/{order_id}   # cancel order
GET  /portfolio/positions             # current positions
GET  /portfolio/balance               # account balance
```

WebSocket channels: `orderbook_delta`, `trade`, `ticker`, `order_fill`

## Coding Conventions

- Python 3.11+, type hints on all function signatures
- `numpy` / `pandas` for numerics; `scipy` for stats
- `aiohttp` or `httpx` for async HTTP; `websockets` for WS
- Prices always stored as integers (0–99); convert to float probability (divide by 100) only at analysis layer
- No magic numbers — name constants
- Raise domain-specific exceptions (`KalshiAPIError`, `InsufficientMarginError`, etc.) rather than generic ones
- No unnecessary abstractions. Three similar lines > premature abstraction.
- No defensive coding for impossible cases. No fallbacks for internal errors.
- All backtests must account for fees and use realistic fill assumptions (never assume mid-fill)

## Domain Context

- Kalshi markets are binary: outcome is 0 or 1, contracts pay $1 on yes
- Fair value = P(yes), quoted as integer 0–99
- Market making edge = half-spread captured; risk = adverse selection + inventory
- For MM, the key quantities are: fair value estimate, spread around fair, inventory position, Greeks (delta = net yes exposure)
- Skew quotes around fair value based on inventory (standard MM inventory model)
- Temperature markets: NOAA data is the primary external data source; NWS forecasts are the primary signal
- Repeatability metric: same market structure recurs daily/weekly — prioritize these for strategy development

## What to Avoid

- Don't paper-trade or assume you can assume fill at any price — Kalshi is thin in many markets
- Don't add error handling for API responses that can't occur given the schema
- Don't create notebook-style analysis code in the strategy or backtest modules
- Don't use `requests` (blocking) in any live trading path — async only
- Don't commit API keys or credentials
- Don't pretend to know something, just say you don't know
- Don't be afraid to ask questions and clarify understanding 

## How to act

- Always reference the project_state.md to save energy
- Treat me as if I am the head of a desk at a quant trading firm such as Jane Street
- You are heading the expansion of our trading to Kalshi
- You have advanced degrees in both Mathematics and Computer Science from top American universities
- Display a rigorous understand of what you are doing but do not fake it
- Ask questions
- Make sure you fully understand what is being asked of you before continueing. Ask clarifying questions. 
- Simple is best. We don't want the project exploding to an unmanagable state

## Fixes to Errors I've seen

- Use python3 as base python command (not 'python')

## How to connect to VPS
- ssh -i ~/.ssh/hetzner root@5.161.111.138
- The directory for this project is /opt/kalshi