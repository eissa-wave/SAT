#!/usr/bin/env python3
"""
SAT Risk Alerts: Monitors Binance USDM futures leverage and equity for the SAT account.
Fires Slack alerts when thresholds are breached.
Uses standard margin endpoints (not portfolio margin).
Designed for Jenkins: credentials via environment variables, failures notify Slack.
"""

import os
import sys
import time
import traceback
import requests
from typing import Any, Dict, List, Optional

from binance.client import Client


BINANCE_API_KEY    = os.environ["BINANCE_API_KEY"]
BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
SLACK_WEBHOOK      = os.environ["SLACK_WEBHOOK"]

LEVERAGE_THRESHOLD = 0
EQUITY_THRESHOLD   = 100000.0


def send_slack_alert(message: str):
    try:
        resp = requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"Slack alert failed: {e}", file=sys.stderr)


def send_slack_error(component: str, error: Exception):
    tb = traceback.format_exception(type(error), error, error.__traceback__)
    short_tb = "".join(tb[-3:])
    message = (
        f":x: *SAT Risk Monitor Failure*\n"
        f"Component: `{component}`\n"
        f"Error: `{type(error).__name__}: {error}`\n"
        f"```{short_tb}```"
    )
    send_slack_alert(message)


def build_binance_client() -> Client:
    return Client(
        api_key=BINANCE_API_KEY,
        api_secret=BINANCE_API_SECRET,
        tld="com",
        testnet=False,
        verbose=False,
    )


def fetch_binance_equity(client: Client) -> float:
    """
    Fetch USDM futures account equity.
    Uses /fapi/v2/account which returns totalWalletBalance and totalUnrealizedProfit.
    """
    resp = client.futures_account(recvWindow=5000)
    wallet = float(resp.get("totalWalletBalance", 0) or 0)
    upnl = float(resp.get("totalUnrealizedProfit", 0) or 0)
    return wallet + upnl


def fetch_binance_positions(client: Client) -> List[Dict[str, Any]]:
    """
    Fetch open USDM futures positions from /fapi/v2/account.
    """
    resp = client.futures_account(recvWindow=5000)
    positions = []
    for p in resp.get("positions", []):
        amt = float(p.get("positionAmt", 0) or 0)
        if amt == 0:
            continue
        entry = float(p.get("entryPrice", 0) or 0)
        upnl = float(p.get("unrealizedProfit", 0) or 0)
        side = "SHORT" if amt < 0 else "LONG"
        notional_entry = abs(amt) * entry
        if side == "LONG":
            notional_mtm = notional_entry + upnl
        else:
            notional_mtm = notional_entry - upnl
        positions.append({
            "symbol": p.get("symbol"),
            "positionAmt": amt,
            "side": side,
            "entryPrice": entry,
            "unrealizedProfit": upnl,
            "notional_mtm": notional_mtm,
        })
    return positions


def compute_gross_leverage(positions: List[Dict[str, Any]], equity: float) -> float:
    if not positions or equity == 0:
        return 0.0
    gross_notional = sum(p["notional_mtm"] for p in positions)
    return gross_notional / equity


def main():
    bn_equity: Optional[float] = None
    bn_positions: Optional[List[Dict[str, Any]]] = None

    try:
        bn_client = build_binance_client()
    except Exception as e:
        send_slack_error("Binance client init", e)
        print(f"FATAL: Binance client init failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        bn_equity = fetch_binance_equity(bn_client)
        bn_positions = fetch_binance_positions(bn_client)
    except Exception as e:
        send_slack_error("Binance API", e)
        print(f"ERROR: Binance API failed: {e}", file=sys.stderr)
        send_slack_alert(
            ":fire: *SAT Risk Monitor*: Binance API call failed. "
            "No risk data available."
        )
        sys.exit(1)

    gross_leverage = compute_gross_leverage(bn_positions, bn_equity)

    print(f"Binance Equity:   ${bn_equity:,.2f}")
    print(f"Gross Leverage:   {gross_leverage:.2f}x")
    print()

    if bn_positions:
        print("Open Positions:")
        for p in sorted(bn_positions, key=lambda x: abs(x["notional_mtm"]), reverse=True):
            print(
                f"  {p['symbol']:>12s}  {p['side']:>5s}  "
                f"qty={p['positionAmt']:>14.4f}  "
                f"notional=${p['notional_mtm']:>14,.2f}  "
                f"uPnL=${p['unrealizedProfit']:>12,.2f}"
            )
        print()

    alerts = []

    if gross_leverage > LEVERAGE_THRESHOLD:
        alerts.append(
            f":warning: *SAT Leverage Alert*: Binance gross leverage is "
            f"{gross_leverage:.2f}x, check positions! "
            f"(threshold: {LEVERAGE_THRESHOLD:.1f}x)"
        )

    if bn_equity < EQUITY_THRESHOLD:
        alerts.append(
            f":rotating_light: *SAT Equity Alert*: Binance account equity is "
            f"${bn_equity:,.2f}, reach out to SAT team immediately! "
            f"(threshold: ${EQUITY_THRESHOLD:,.2f})"
        )

    if alerts:
        message = "\n".join(alerts)
        print(f"\nFiring {len(alerts)} alert(s)...")
        send_slack_alert(message)
    else:
        print("\nNo alerts triggered.")


if __name__ == "__main__":
    main()
