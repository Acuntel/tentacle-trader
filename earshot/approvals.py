"""Human-in-the-loop over the bot's self-improvement — via Telegram.

The bot proposes a concrete, gateable change to itself (a code patch, a config/threshold shift, or a
new-source toggle). Each proposal is texted to Brady with Approve / Deny buttons. When he taps:

  APPROVE -> the exact patch is handed to the `apply-approved` GitHub Action via repository_dispatch.
             The Action applies it, runs the SAME safety gate selfcode uses (compile + import + sim),
             commits to main, and redeploys Railway. The money container never writes its own code.
  DENY    -> the proposal is dropped AND remembered, so the coach won't re-propose the same idea and
             down-weights that direction (it learns from the 'no').

Bounded weight/lesson micro-tuning still auto-applies (gating every 0.94x nudge would be spam); those
are summarized in a once-a-day digest message instead. The 3 rails (risk caps, money gate, Gauntlet)
are refused by the apply path regardless of any approval.

State rides inside knowledge.json (know['approvals']) so pending items survive every redeploy.
Degrades to a silent no-op when Telegram / the dispatch token aren't set.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

try:
    import requests
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

from . import notify
from .models import iso

QUEUE_FALLBACK = "state/evolution/approved_queue.jsonl"   # used if no dispatch token is set


def _hash(kind: str, title: str, patches) -> str:
    return hashlib.sha1((kind + "|" + title + "|" + json.dumps(patches, sort_keys=True)).encode()).hexdigest()[:12]


class ApprovalQueue:
    def __init__(self, cfg: dict, know: dict, persist=None):
        a = cfg.get("approvals", {})
        t = cfg.get("telegram", {})
        self.cfg = cfg
        self.on = bool(a.get("enabled", True)) and bool(t.get("enabled", True))
        self.auto_deploy = bool(a.get("auto_deploy", True))
        self.max_per_day = int(t.get("max_proposals_per_day", 4))
        self.digest_on = bool(t.get("daily_digest", True))
        self.know = know
        self.persist = persist or (lambda: None)
        self.st = know.setdefault("approvals", {})
        self.st.setdefault("pending", {})       # id -> item
        self.st.setdefault("log", [])           # decided items (most recent last)
        self.st.setdefault("next", 1)
        self.st.setdefault("seen", {})          # content hash -> id (dedupe)
        self.st.setdefault("denied", {})        # content hash -> 1 (never re-propose)
        self.st.setdefault("denied_sources", [])
        self.st.setdefault("day", "")
        self.st.setdefault("day_count", 0)
        self.st.setdefault("last_digest_day", "")

    # ---- proposing -------------------------------------------------------
    def is_denied_source(self, name: str) -> bool:
        return name in self.st["denied_sources"]

    def _budget_ok(self) -> bool:
        day = time.strftime("%Y-%m-%d")
        if self.st["day"] != day:
            self.st["day"], self.st["day_count"] = day, 0
        return self.st["day_count"] < self.max_per_day

    def propose(self, kind: str, title: str, summary: str, patches=None, meta=None) -> str | None:
        """Queue a change for approval. kind in {code, config, source}. patches = [{file,find,replace}]
        for code/config; None for a new-source-needs-wiring note. Returns the id, or None if disabled,
        a duplicate, already denied, or over the daily cap."""
        if not (self.on and notify.enabled()):
            return None
        h = _hash(kind, title, patches)
        if h in self.st["seen"] or h in self.st["denied"]:
            return None
        if not self._budget_ok():
            return None
        cid = f"A{self.st['next']:04d}"
        self.st["next"] += 1
        item = {"id": cid, "hash": h, "kind": kind, "title": title, "summary": summary,
                "patches": patches, "meta": meta or {}, "ts": iso(time.time())}
        self.st["seen"][h] = cid
        self.st["day_count"] += 1
        if notify.shared():
            # Nessobot (the existing content bot) owns the inbound loop. STAGE the patch to GitHub now,
            # then send the card WITH buttons via Nessobot's token — Nessobot relays the tap to the
            # apply/discard Action (the decision never has to reach this process).
            if patches:
                item["staged"] = bool(self._dispatch_event(
                    "stage-approval", {"id": cid, "title": item["title"][:120],
                                       "patches_json": json.dumps(patches)[:60000]}))
                mid = notify.send(self._render(item),
                                  buttons=[("✅ Approve", f"ok:{cid}"), ("❌ Deny", f"no:{cid}")])
            else:  # a brand-new source with no safe auto-patch -> informational text only
                mid = notify.send(self._render(item, interactive=False))
            item["message_id"] = mid
            self.st["pending"][cid] = item          # tracked; resolved by the relay Actions, not here
            self.persist()
            return cid
        self.st["pending"][cid] = item
        mid = notify.send(self._render(item),
                          buttons=[("✅ Approve", f"ok:{cid}"), ("❌ Deny", f"no:{cid}")])
        item["message_id"] = mid
        self.persist()
        return cid

    PRETTY = {
        "min_pwin": "minimum win-chance I need before I'll trade",
        "min_score_to_trade": "conviction bar I need to trade",
        "min_independent_sources": "number of independent sources I need to agree",
        "min_families": "number of different signal types I need to agree",
        "daily_loss_pct": "daily loss limit before I stop for the day",
        "regime_downtrend_premium": "extra caution when the market's falling",
        "max_runup_pct": "how far a stock can already have moved before I skip it",
        "every_minutes": "how often I re-tune myself",
        "trail_pct": "how much profit I give back before I lock in a win",
        "walk_forward_folds": "how many time periods I test a strategy across",
    }

    def _pretty(self, setting) -> str:
        key = str(setting).split(".")[-1]
        return self.PRETTY.get(key, key.replace("_", " "))

    def _render(self, item: dict, interactive: bool = True) -> str:
        icon = {"code": "🧬", "config": "🎚️", "source": "📡"}.get(item["kind"], "🛠️")
        m = item.get("meta") or {}
        head = m.get("plain") or item.get("title") or "a change to myself"
        lines = [f"{icon} <b>I want to make a change</b>  <code>{item['id']}</code>", "",
                 notify.esc(head)]
        if m.get("why"):
            lines += ["", f"<b>Why:</b> {notify.esc(m['why'])}"]
        if m.get("tech"):
            lines += ["", f"<i>under the hood: {notify.esc(m['tech'])}</i>"]
        if interactive:
            tail = ("Tap ✅ and I'll test it on a clean run, then ship it live if it passes."
                    if self.auto_deploy else
                    "Tap ✅ and I'll test it and merge it (you deploy when ready).")
            tail += " My real-money lock stays ON either way."
        else:
            tail = "ℹ️ Just a heads-up — I can't safely apply this one on my own yet."
        lines += ["", tail]
        return "\n".join(lines)

    # ---- handling taps ---------------------------------------------------
    def process(self) -> list[str]:
        """Poll Telegram for taps and apply/deny. Returns short lines to print in the loop."""
        if not (self.on and notify.enabled()):
            return []
        out = []
        for tap in notify.poll():
            data = tap.get("data", "")
            if ":" not in data:
                continue
            verb, cid = data.split(":", 1)
            item = self.st["pending"].pop(cid, None)
            if not item:
                notify.ack(tap["query_id"], "expired")
                continue
            decided = "APPROVED" if verb == "ok" else "DENIED"
            item["decision"] = decided
            item["decided_ts"] = iso(time.time())
            self.st["log"] = (self.st["log"] + [item])[-200:]
            notify.ack(tap["query_id"], "✅ approved" if verb == "ok" else "❌ denied")
            if verb == "ok":
                res = self._apply(item)
            else:
                res = self._deny(item)
            notify.edit(tap.get("chat_id"), item.get("message_id"),
                        f"{'✅' if verb == 'ok' else '❌'} <b>{decided}</b> {notify.esc(item['title'])}\n\n{notify.esc(res)}")
            out.append(f"telegram: {item['id']} {decided} — {res}")
            self.persist()
        return out

    def _apply(self, item: dict) -> str:
        patches = item.get("patches")
        if not patches:                                  # a genuinely-new source the bot can't self-wire
            self.know.setdefault("accepted_wiring", []).append(item["title"])
            return "accepted — needs human wiring (no safe auto-patch exists for a brand-new source)"
        if self._dispatch_event("apply-approved",
                                {"id": item["id"], "title": item["title"][:120],
                                 "auto_deploy": "1" if self.auto_deploy else "0",
                                 "patches_json": json.dumps(patches)[:60000]}):
            return "dispatched to the apply Action → gate + " + \
                   ("merge + redeploy" if self.auto_deploy else "merge")
        # no dispatch token: save it so it can be applied manually / by the scheduled Action
        os.makedirs(os.path.dirname(QUEUE_FALLBACK) or ".", exist_ok=True)
        with open(QUEUE_FALLBACK, "a") as f:
            f.write(json.dumps({"ts": iso(time.time()), "title": item["title"], "patches": patches}) + "\n")
        return "approved — saved to approved_queue.jsonl (set GITHUB_DISPATCH_TOKEN to auto-apply)"

    def _deny(self, item: dict) -> str:
        self.st["denied"][item["hash"]] = 1
        for s in (item.get("meta") or {}).get("sources", []):
            if s not in self.st["denied_sources"]:
                self.st["denied_sources"].append(s)
        return "dropped and remembered — I won't propose this again and I down-weight that direction"

    def _dispatch_event(self, event_type: str, payload: dict) -> bool:
        """Fire a GitHub repository_dispatch to the trader's repo (used to stage/apply/discard patches
        on isolated runners — the money container never writes code)."""
        tok = os.getenv("GITHUB_DISPATCH_TOKEN")
        repo = os.getenv("GITHUB_REPO", "Acuntel/tentacle-trader")
        if not (_HAVE and tok):
            return False
        try:
            r = requests.post(f"https://api.github.com/repos/{repo}/dispatches",
                              json={"event_type": event_type, "client_payload": payload},
                              headers={"Authorization": f"Bearer {tok}",
                                       "Accept": "application/vnd.github+json",
                                       "X-GitHub-Api-Version": "2022-11-28"}, timeout=20)
            return r.status_code in (200, 204)
        except Exception:
            return False

    # ---- the once-a-day visibility digest (auto-applied stuff, no approval) ----
    def daily_digest(self, text: str):
        if not (self.digest_on and notify.enabled()):
            return
        day = time.strftime("%Y-%m-%d")
        if self.st["last_digest_day"] == day:
            return
        self.st["last_digest_day"] = day
        notify.send("🐙 <b>TENTACLE daily self-report</b>\n\n" + notify.esc(text))
        self.persist()

    def startup_ping(self, text: str):
        if notify.enabled():
            notify.send("🟢 <b>TENTACLE online</b>\n\n" + notify.esc(text))
