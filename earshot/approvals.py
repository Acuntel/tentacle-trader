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
            # Nessobot owns the inbound loop, so we can't receive taps here. Notify as TEXT only (no
            # dead buttons); the approval relay is wired separately. Don't leave an un-tappable pending.
            notify.send(self._render(item, interactive=False))
            item["status"] = "notified (shared bot — interactive approval relay not wired yet)"
            self.st["log"] = (self.st["log"] + [item])[-200:]
            self.persist()
            return cid
        self.st["pending"][cid] = item
        mid = notify.send(self._render(item),
                          buttons=[("✅ Approve", f"ok:{cid}"), ("❌ Deny", f"no:{cid}")])
        item["message_id"] = mid
        self.persist()
        return cid

    def _render(self, item: dict, interactive: bool = True) -> str:
        icon = {"code": "🧬", "config": "🎚️", "source": "📡"}.get(item["kind"], "🛠️")
        lines = [f"{icon} <b>TENTACLE wants to change itself</b>  <code>{item['id']}</code>",
                 f"<b>{notify.esc(item['title'])}</b>", "", notify.esc(item["summary"])]
        for p in (item.get("patches") or [])[:3]:
            f = notify.esc(p.get("file", "?"))
            find = notify.esc(str(p.get("find", ""))[:160])
            repl = notify.esc(str(p.get("replace", ""))[:160])
            lines += ["", f"📄 <code>{f}</code>", f"<i>−</i> <code>{find}</code>",
                      f"<i>+</i> <code>{repl}</code>"]
        if interactive:
            tail = ("Approve → tested by the gate, merged & redeployed live." if self.auto_deploy
                    else "Approve → tested by the gate & merged to main (you deploy).")
            tail += "  Real-money gate stays locked regardless."
        else:
            tail = "ℹ️ Heads-up: a change I want to make. Interactive ✅/❌ through Nessobot is being wired."
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
        if self._dispatch(item, patches):
            return "dispatched to the apply-approved Action → gate + " + \
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

    def _dispatch(self, item: dict, patches) -> bool:
        tok = os.getenv("GITHUB_DISPATCH_TOKEN")
        repo = os.getenv("GITHUB_REPO", "Acuntel/tentacle-trader")
        if not (_HAVE and tok):
            return False
        payload = {"title": item["title"][:120], "id": item["id"],
                   "auto_deploy": "1" if self.auto_deploy else "0",
                   "patches_json": json.dumps(patches)[:60000]}
        try:
            r = requests.post(f"https://api.github.com/repos/{repo}/dispatches",
                              json={"event_type": "apply-approved", "client_payload": payload},
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
