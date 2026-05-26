# ETH Positioning Dashboard — Obsidian Vault

This directory is an **Obsidian vault** documenting the architecture, data sources, computed metrics, and key design decisions of the ETH positioning dashboard.

## How to open it

1. Install [Obsidian](https://obsidian.md)
2. **File → Open vault → Open folder as vault**
3. Navigate to `<repo>/docs/obsidian/`
4. Start at [[Home]]

## Why a vault and not just markdown files

The notes here are heavily cross-linked. Obsidian renders `[[wiki-links]]` as real navigable links, shows a backlinks panel ("which notes reference this one"), and offers a graph view that surfaces clusters of related concepts (e.g. all ADRs that touch the hedge ratio).

You can still read the files in any editor — they're plain markdown — but you lose the navigation.

## What's inside

| Folder | Content |
|---|---|
| `01-architecture/` | High-level layout: two-backend mirror, frontend, deploy targets |
| `02-data-sources/` | One note per upstream API (Dune, Etherscan, Binance, etc.) — gotchas + auth + rate limits |
| `03-metrics/` | Computed signals: z-score, hedge ratio, liq map, whale-vs-retail divergence |
| `04-decisions/` | ADRs (Architecture Decision Records). The **why** behind non-obvious choices |
| `05-operations/` | Runbook: env vars, deploy, smoke tests, quota recovery |

Start with [[Home]] — it has the index.
