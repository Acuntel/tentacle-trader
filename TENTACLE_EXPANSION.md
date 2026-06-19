# EARSHOT — Tentacle Expansion Masterprompt
### How to keep growing the data surface with FREE sources, by scraping the *hidden* databases

> Goal: more tentacles that actually carry signal. The trick isn't more news sites — it's reaching
> the **structured data behind sites** (their private JSON APIs), and the **obscure free datasets**
> quant funds pay for. Everything here is free. Model every scraper on Brady's pattern:
> `requests.Session` + spoofed Chrome UA + randomized polite delays + failure tolerance + cache.

---

## 0. The one technique that matters: scrape the hidden database, not the HTML

When a webpage shows a table of data, that data almost always arrives from a **JSON/XHR endpoint**
the page calls in the background. Hit that endpoint directly and you get clean, structured data —
no HTML parsing, far more fields than the UI shows. This is "scraping into their database."

**How to find it (do this for any site with data we want):**
1. Open the page in Chrome → **F12 → Network → Fetch/XHR** → reload.
2. Watch the requests. Look for ones returning JSON (`api.`, `/api/`, `query2.`, `.json`, `graphql`).
3. Click it → **Copy → Copy as cURL** → replicate in Python with the same headers.
4. Strip cookies; keep only what's required (usually just a `User-Agent`, sometimes `Referer`/`Origin`
   or a public token embedded in the page's JS — grep the page source for it).
5. Vary the ticker/date in the URL → now it's a per-company tentacle.

**Other recon shortcuts:** check `robots.txt` and `sitemap.xml` (reveal hidden paths), look for
`/rss`, `/feeds/`, `?format=json`, `?output=atom`, and undocumented-but-public APIs (Nasdaq, Yahoo).

---

## 1. The catalog — free "nooks and crannies," ranked by signal value

### TIER A — Structured fundamentals & per-company databases (highest value)
| Source | Hidden endpoint / how | What it predicts | Auth |
|---|---|---|---|
| **SEC EDGAR `companyfacts`** | `data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json` | EVERY financial metric a company ever filed (revenue, margin, debt, EPS) — fundamentals trend | free, UA only |
| **SEC `frames`** | `data.sec.gov/api/xbrl/frames/us-gaap/{Concept}/USD/CY{q}.json` | one metric across ALL companies in a period (cross-sectional screens) | free |
| **Nasdaq private API** | `api.nasdaq.com/api/quote/{T}/...`, `/analyst/{T}/price-target`, `/company/{T}/insider-trades`, `/company/{T}/short-interest` | earnings dates, EPS, price targets, insider, **short interest** | free, UA |
| **Yahoo quoteSummary** | `query2.finance.yahoo.com/v10/finance/quoteSummary/{T}?modules=earningsTrend,recommendationTrend,insiderTransactions,upgradeDowngradeHistory,institutionOwnership` | analyst revisions, insider flow, institutional ownership | free (may need a crumb) |
| **StockTwits** | `api.stocktwits.com/api/2/streams/symbol/{T}.json` | message volume + bull/bear sentiment per name | free |

### TIER B — Government / regulatory open data (free, authoritative, under-watched)
| Source | Endpoint | Signal |
|---|---|---|
| **USPTO PatentsView** | `api.patentsview.org` (POST query) | patent grants per company → innovation/pipeline |
| **openFDA** | `api.fda.gov/drug/...`, `/device/...`, `/...enforcement` (recalls) | approvals, recalls, adverse events |
| **EIA energy** | `api.eia.gov` (free key) | oil/gas/electric inventories → energy names |
| **NHTSA** | `api.nhtsa.gov/recalls/...` | auto recalls → automakers/suppliers |
| **CourtListener / PACER** | `courtlistener.com/api/rest/v4/` | lawsuits, bankruptcies, patent litigation |
| **SAM.gov / USASpending** | (USASpending wired) + SAM contract opportunities | gov contract pipeline |
| **Federal Register** | `federalregister.gov/api/v1/` | rules/approvals affecting sectors |
| **FCC / FAA / USDA / Census trade** | various JSON APIs | sector-specific catalysts, import/export volume |

### TIER C — Web / attention alt-data (the "site visitors → earnings" family)
| Source | How | Signal |
|---|---|---|
| **Wikipedia pageviews** (wired) | wikimedia REST | attention spike → demand/earnings |
| **Wikidata** | `query.wikidata.org/sparql` | entity facts, ticker↔company↔domain mapping |
| **Google Trends** | `pytrends` (unofficial) | search interest → consumer demand |
| **GitHub / HuggingFace / PyPI** | trending APIs, `pypistats` | dev adoption → SaaS/AI revenue |
| **Greenhouse / Lever ATS** | `boards-api.greenhouse.io/v1/boards/{co}/jobs`, `api.lever.co/v0/postings/{co}` | **hiring velocity per company** → growth (free, per-company JSON!) |
| **Downdetector / status pages** | scrape | outages → service/revenue risk |
| **Wayback Machine CDX** | `web.archive.org/cdx/search/cdx?url={site}` | detect pricing/product page changes → launches |
| **Steam / Twitch / App stores** | charts APIs | gaming/streaming/app demand |

### TIER D — Physical & "exhaust" data (free proxies for what funds buy)
| Source | How | Signal |
|---|---|---|
| **ADS-B Exchange** | flight API | corporate-jet spotting → M&A |
| **AIS marine traffic** | free tiers | shipping/port flow → industrials/retail |
| **NOAA weather** | `api.weather.gov` | energy/ag/retail/insurer demand |
| **USGS earthquakes** | `earthquake.usgs.gov/.../summary` | insurer/reinsurer risk |
| **ImportYeti / bill-of-lading** | scrape | supplier/import volume per company |

### TIER E — Sentiment / flow / forecast (context, lower weight)
Reddit (ApeWisdom wired), Hacker News (wired), Polymarket (wired), Kalshi, Metaculus, Manifold,
Fed/FOMC speeches, CME FedWatch odds, crypto/onchain risk proxies.

---

## 2. Map every source to the companies we're watching
For each ticker in the **fast core watchlist**, run per-company probes against the structured
endpoints above → a rich feature row per name, refreshed on a sane cadence:
`SEC companyfacts (fundamentals) · Nasdaq short-interest + price-targets · Yahoo analyst revisions ·
StockTwits sentiment · Greenhouse job count · Wikipedia/Trends attention · patents · recalls/FDA`.
Resolve ticker→CIK via SEC `company_tickers.json` (already loaded), ticker→domain via Wikidata,
ticker→ATS-slug via a small curated map (grows over time, like the scraper's name map).

---

## 3. How to add a tentacle (3 code patterns, all already supported)
1. **Hidden-JSON source** — new `Source` subclass: `requests.get(json_endpoint)` → map fields →
   `RawSignal(source=..., tickers=[T], catalyst_hint=..., strength=...)`. Set `min_interval` to the
   data's real update cadence (intraday vs daily vs quarterly).
2. **Generic HTML scrape** — add `{url, selector, src_key, strength}` to `config.sources.scrape_targets`
   (`HtmlScraperSource` already handles it — zero new code).
3. **RSS/feed** — drop the URL into the fleet (`fleet.py`) or `rss_feeds`.

Then: add the source key to `config.source_weights` + `brain.FAMILY` (pick its family so it counts
toward cross-family convergence), and the pattern brain + learning loop pick it up automatically.

---

## 4. Weighting, families & the pattern brain
- New structured/primary sources (SEC, Nasdaq, patents, FDA) → **own families**, mid/high weight,
  may help *trigger*. Attention/sentiment/social → low weight, **confirmation only**.
- Every new source becomes a **pattern feature** — the brain learns which *combinations* across
  fundamentals + attention + macro + flow actually win, and the learning loop tunes each source's
  weight by its realized track record. **Adding data only helps if it earns its weight.**

## 5. Guardrails (non-negotiable, at any scale)
- **Rate limits:** respect each API (FRED 120/min, SEC 10/s, Nasdaq/Yahoo gentle). Rotate + cache.
- **Politeness/ToS:** spoofed UA + randomized delays + per-source failure tolerance; honor robots
  where required; never hammer one domain (rotating batches, like the megafleet).
- **Liquidity gate stays:** no matter how much data, the risk gate only trades liquid names — junk
  data can't cause a junk trade.
- **No silent noise:** a source that never beats a coin flip gets down-weighted, then vetoed.

## 6. Priority roadmap (highest value first)
1. **SEC `companyfacts`** per watched ticker — real fundamentals trend (revenue/margin acceleration).
2. **Nasdaq short-interest + price-target** API — squeeze setups + analyst pressure.
3. **Greenhouse/Lever hiring velocity** — cleanest free "growth" proxy per company.
4. **Yahoo quoteSummary** analyst revisions + insider flow.
5. **USPTO patents / openFDA / EIA** by sector.
6. **Google Trends + Wayback change-detection** — demand + product-launch detection.

> Build order favors signal-per-token: Tier A first (structured, per-company, predictive), then B,
> then attention/alt-data for breadth. The bot keeps cooking in shadow throughout; each new tentacle
> just gives the pattern brain more to learn from before it ever risks a real dollar.
