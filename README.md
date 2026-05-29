# Electricity Dashboard

Real-time electricity cost & earnings tracker for Sparreholmsvagen 6a.
Growatt solar/battery data + Ellevio tariff + Fortum/Nordpool spot prices.

## Requirements

- macOS with Python 3 (built-in, no installs needed)
- Internet connection
- Growatt ShinePhone account

## Setup

1. **Create your credentials file:**
   ```
   cp .env.example .env
   ```
   Edit `.env` and fill in your ShinePhone email and password.

2. **Start the proxy:**
   ```
   python3 proxy.py
   ```

3. **Open the dashboard:**
   Open http://localhost:8080 in your browser.

4. **Stop:** Press `Ctrl+C` in the terminal.

## Tariff configuration

All tariff values are editable directly in the dashboard UI:
- Ellevio fast avgift (currently 545 kr/mån from 1 June 2025)
- Nätavgift import (öre/kWh)
- Nätnytta export (öre/kWh) — check your Ellevio agreement
- Energiskatt (54.875 öre/kWh, fixed by law)
- Fortum påslag (0 öre if pure spot passthrough)

## Cost formula

**Import cost per kWh:**
  (spot + nätavgift + energiskatt + fortum_påslag) × 1.25 (moms)

**Export earning per kWh:**
  spot + nätnytta  (no moms on income)

**Daily fixed:**
  fast_avgift × 1.25 ÷ 30

## Files

- `proxy.py`   — local proxy server (pure Python stdlib)
- `index.html` — dashboard (auto-served by proxy)
- `.env`        — your credentials (never commit this)
- `.gitignore`  — excludes .env and other sensitive files
