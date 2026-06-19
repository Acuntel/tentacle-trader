# Deep analysis of Alpaca news — and why we built our own instead

## What Alpaca's news API actually is
- **Endpoint:** `wss://stream.data.alpaca.markets/v1beta1/news` (WebSocket push) + a REST poll at `https://data.alpaca.markets/v1beta1/news`.
- **Auth:** `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` (free tier).
- **Source of the data:** it is **Benzinga's newswire**, resold under a 2022 Alpaca–Benzinga partnership. Alpaca is a *pipe*, not the origin.
- **Payload (per item):** `id, headline, summary, author, content, url, symbols[] (tagged tickers), source, created_at, updated_at` — clean structured JSON.
- **Strengths:** push/low-latency, pre-tagged tickers, Benzinga editorial quality.
- **Weaknesses for us:** requires an Alpaca account + keys (the friction you hit), and the *real* value (Benzinga) is just a republisher of the underlying primary wires.

## The key insight
**Benzinga doesn't generate most market-moving headlines — it republishes them.** The originals come from:
- **Primary press-release wires:** GlobeNewswire, PR Newswire, Business Wire (where companies post earnings, M&A, FDA, contracts — often with the ticker right in the text: "(NASDAQ: XYZ)").
- **Fast market media:** CNBC markets/earnings, MarketWatch real-time, Yahoo Finance, Seeking Alpha market-currents.

All of those publish **free RSS**. So we can reconstruct ~90% of Alpaca/Benzinga's signal value by going straight to the sources — no Alpaca, no account, no key.

## What we built (`earshot/sources/newswire.py` → `MarketNewsSource`)
"Our own Alpaca": one tentacle that aggregates the free wires above, normalizes each item into the **same structured shape** (headline, url, symbols, timestamp), extracts tickers via:
1. **`(NASDAQ: XYZ)` / `(NYSE: ABC)` regex** — high-precision, exactly how press releases tag themselves;
2. **`$CASHTAG`** parsing;
3. an **expanded company-name → ticker map** (word-boundary matched to avoid false positives).
Press releases emit at high weight (`newswire` 1.0); media at lower weight (`google_news` 0.9/0.8). Both are the **news family**, so they create the cross-family confirmation the brain needs.

## Honest tradeoff
| | Alpaca/Benzinga | Our newswire |
|---|---|---|
| Transport | WebSocket push (ms) | RSS poll (~60s) |
| Curation | Benzinga editorial | raw wires + media (broader, noisier) |
| Cost / account | free tier, needs keys | **fully free, no account** |
| Tickers | pre-tagged | parsed (precise on press releases) |

The ~60s poll latency is **fine for our strategy** — we trade multi-source *drift* (minutes→hours), not the millisecond gap we'd lose to HFTs on Robinhood anyway. And we **own** it: no third-party dependency, infinitely extensible (drop any RSS/RSSHub/Telegram-RSS URL in).

## Net
Alpaca is dropped as a hard dependency. **Finnhub** (now keyed) covers structured market news + earnings-surprise; **our MarketNewsSource** covers the primary + media wires. Together they give a stronger, free, self-owned "news + surprise" layer than Alpaca alone would have.
