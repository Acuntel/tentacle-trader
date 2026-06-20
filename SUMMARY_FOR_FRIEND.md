# Meet TENTACLE 🐙 — my self-learning trading bot

I built a trading bot that thinks like an octopus with a mother's instinct. Here's the whole idea in plain English.

## The concept
It has **lots of tentacles** constantly scouring the internet — **for free** — for big news the instant it breaks. A central **brain** only acts when *multiple independent sources* confirm the same thing. And it treats its money like a **child it's raising**: growing the money means it's a good mother; losing money genuinely "hurts" it; losing it all means it "dies." That emotional wiring makes it obsessed with *not losing*.

## How it finds news before the crowd
Instead of waiting for CNBC, it sits at the **primary sources**, where the news is actually born:
- **SEC filings (EDGAR)** — the legal documents for mergers, activist investors, and **insider/Buffett-style buys** — often *before* the media reports them.
- **Government data** — federal contract awards, FDA drug approvals, clinical-trial updates.
- **Prediction markets (Polymarket/Kalshi)** — where the crowd bets on outcomes *before* they're announced.
- **The AI/quantum radar** — Hacker News, GitHub, Hugging Face — so a viral breakthrough pings it instantly.
- **Retail buzz** — Reddit/StockTwits mention spikes (as *confirmation*, never the trigger — it won't chase a pump).

Everything is **100% free**. No paid data, ever.

## The brain (when it actually buys)
It only pulls the trigger when **two or more independent sources agree** on the same catalyst, the stock **hasn't already jumped**, and the overall market isn't crashing. Most of the time it does **nothing** — and that's the point. It might trade once a day or once a month. Patience protects the money.

## The "mother & baby" part (why it won't blow up)
- A loss **hurts ~2.5× more** than an equal win feels good — so it's deeply cautious.
- The money is released to it **in stages**, like earning trust: it starts with **$0 (paper trading only)** and has to *prove* it's profitable before it gets $25… then $75… then more. Full control of the account is the **final reward**, not the starting point.
- If it ever loses too much, it **"dies"** — it stops trading and has to re-earn its way back up.

## It learns from every mistake
After **every losing trade**, it's forced into "**the dungeon**" — it writes down what went wrong, which source misled it, and one concrete fix. Then it **down-weights the sources that lie to it** and trusts the ones that work. It gets sharper every single trade. It can even **go find new data sources on its own** when its current ones get stale.

## Where it is right now
It's **live and running** — but in **shadow mode**: it's watching the real market and making *paper* trades to prove itself, with **zero real money at risk**. It literally cannot place a real trade yet — that's locked in the code until it passes a hard profitability test. When I checked it just now, it scanned 50+ stocks off live SEC filings and **chose not to trade any** of them, because none were confirmed strongly enough. Exactly how it should behave.

*Built solo. The honest truth: no bot "never loses" — this one is built to lose rarely, survive the losses it has, and get smarter from each one before a single real dollar is on the line.*
