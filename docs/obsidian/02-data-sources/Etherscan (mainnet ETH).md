---
tags: [data-source, etherscan]
---

# Etherscan (mainnet ETH)

Used to fetch the **mainnet L1 ETH balance** for each curated whale wallet, so the [[Hedge ratio + label]] calc reflects real spot custody, not just HL UETH.

## Endpoint

```
GET https://api.etherscan.io/v2/api?chainid=1&module=account&action=balancemulti&address=A,B,C,...&tag=latest&apikey=KEY
```

Fetcher: `fetch_etherscan_eth_balances` (mirrored in both [[Two-headed backend|backends]]).

## V2 is mandatory — V1 was deprecated

If you hit `https://api.etherscan.io/api?...` (no `/v2/`, no `chainid=`), it returns:

```json
{ "status": "0", "message": "NOTOK",
  "result": "You are using a deprecated V1 endpoint, switch to Etherscan API V2 using https://docs.etherscan.io/v2-migration" }
```

So you must use `/v2/api` AND include `chainid=1` (Ethereum mainnet) as a query param. Both required.

## Why `balancemulti`

Rate limit on free tier is 5 req/s, 100k req/day. With 5–20 whales we could burn the per-second quota with naive per-address calls. `balancemulti` returns up to 20 balances in 1 request. Batching > parallelism here.

## Where it lands in the payload

`hyperliquidWhales.positions[*].mainnetEth` — float, ETH (not wei). Fed into:

- `totalSpotEth = ueth_spot + mainnet_eth`
- `hedge_ratio = total_spot_eth / perp_size_eth` (capped at 1.0 for SHORT positions)
- `hedge_label` (see [[Hedge ratio + label]])

If `ETHERSCAN_API_KEY` is unset, the fetch returns `{}` and the field falls back to `0.0`. No crash, just a silently-wrong hedge label (everything becomes `DIRECTIONAL_BET` because spot looks empty).

## Gotcha: trading wallets ≠ custody wallets

Most whales use **separate addresses** for their HL trading vs their long-term ETH custody. The whale list seeded in env (`HYPERLIQUID_WHALE_ADDRESSES`) by default contains addresses known from Onchain Lens posts — those are **trading** addresses. They show `~0.0002 ETH` (operational gas) on mainnet, not the multi-thousand-ETH custody.

To make hedge detection actually work, you need to **trace deposit flows on Etherscan** to find the source wallet that bridged ETH to the trading address, then add THAT to the curated list.

## See also

- [[Hyperliquid]]
- [[Hedge ratio + label]]
- [[Environment variables]]
- [[ADR-004 Hedge uses HL UETH plus L1 mainnet]]
