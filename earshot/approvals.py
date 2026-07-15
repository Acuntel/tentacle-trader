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
        self.min_interval = int(t.get("min_interval_min", 90)) * 60   # space cards out across the day
        self.digest_on = bool(t.get("daily_digest", True))
        self.autonomous = bool(a.get("autonomous", False))            # apply + NOTIFY (no ✅/❌ questions)
        self.knob_cooldown = int(a.get("knob_cooldown_min", 360)) * 60  # don't re-tune the same knob this often
        self.max_inflight = int(a.get("max_unscored_in_flight", 2))     # attribution pacing (see propose())
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
        self.st.setdefault("tuned", {})         # config-knob -> last-tuned ts (anti-oscillation)
        self.st.setdefault("last_sent_ts", 0)
        self.st.setdefault("last_digest_day", "")

    # ---- proposing -------------------------------------------------------
    def is_denied_source(self, name: str) -> bool:
        return name in self.st["denied_sources"]

    def _budget_ok(self) -> bool:
        day = time.strftime("%Y-%m-%d")
        if self.st["day"] != day:
            self.st["day"], self.st["day_count"] = day, 0
        return (self.st["day_count"] < self.max_per_day and
                (time.time() - self.st.get("last_sent_ts", 0)) >= self.min_interval)

    @staticmethod
    def _knob(patches):
        """The thing a patch tunes (for the anti-oscillation cooldown). Config: the yaml key.
        AUDIT FIX — code patches too: file + the digit-stripped find snippet, so re-tuning the SAME
        LINE's constants trips the cooldown (8 approvals ratcheted one tanh scale 2->64 in 2 days
        because .py patches had no cooldown key at all)."""
        import re as _re
        for p in (patches or []):
            f = str(p.get("file", ""))
            fnd = str(p.get("find", ""))
            if f.endswith("config.yaml"):
                if ":" in fnd:
                    return fnd.split(":", 1)[0].strip()
            elif f.endswith(".py") and fnd:
                return f.rsplit("/", 1)[-1] + "|" + _re.sub(r"[\d.]+", "#", fnd)[:80]
        return None

    @staticmethod
    def _knob_value(patches):
        """The NEW value a config patch sets a knob to (for the anti-FLIP-BACK memory). e.g. a
        'min_pwin: 0.45' -> 'min_pwin: 0.40' patch returns '0.40'. None for code/source patches."""
        for p in (patches or []):
            if str(p.get("file", "")).endswith("config.yaml"):
                rep = str(p.get("replace", ""))
                if ":" in rep:
                    return rep.split(":", 1)[1].split("#")[0].strip()
        return None

    def propose(self, kind: str, title: str, summary: str, patches=None, meta=None) -> str | None:
        """Queue a change for approval. kind in {code, config, source}. patches = [{file,find,replace}]
        for code/config; None for a new-source-needs-wiring note. Returns the id, or None if disabled,
        a duplicate, already denied, or over the daily cap."""
        if not (self.on and notify.enabled()):
            return None
        h = _hash(kind, title, patches)
        if h in self.st["seen"] or h in self.st["denied"]:
            return None
        knob = self._knob(patches)
        if knob and (time.time() - self.st.get("tuned", {}).get(knob, 0)) < self.knob_cooldown:
            return None                                  # just tuned this knob — don't thrash / re-ask it
        # ANTI-FLIP-BACK: refuse to set a knob to a value it was recently set to (kills A->B->A oscillation,
        # the "changes itself back" Brady saw — e.g. min_fold_winrate 0.5->0.6->0.4->0.6...).
        val = self._knob_value(patches) if knob else None
        if knob and val and val in self.st.setdefault("tuned_values", {}).get(knob, []):
            return None
        if not self._budget_ok():
            return None
        cid = f"A{self.st['next']:04d}"
        self.st["next"] += 1
        item = {"id": cid, "hash": h, "kind": kind, "title": title, "summary": summary,
                "patches": patches, "meta": meta or {}, "ts": iso(time.time())}
        self.st["seen"][h] = cid
        self.st["day_count"] += 1
        self.st["last_sent_ts"] = time.time()
        if knob:
            self.st.setdefault("tuned", {})[knob] = time.time()
            if val:                                      # remember recent values so it can't flip back to them
                tv = self.st.setdefault("tuned_values", {}).setdefault(knob, [])
                tv.append(val)
                self.st["tuned_values"][knob] = tv[-6:]
        # ATTRIBUTION PACING (verdict-study lesson, 2026-07-08): changes applied in bursts share one
        # measurement window and can never be attributed (22 verdicts were identical clones). Cap the
        # UNSCORED changes in flight — each change earns its 24h verdict before the next lands, so the
        # scoreboard->lessons->proposals loop compounds. Blocked ideas stay pooled (emit_due retries).
        if self.autonomous and patches:
            unscored = [c for c in self.know.get("self_changes", []) if not c.get("verdict")]
            if len(unscored) >= self.max_inflight:
                return None
        # W3.5b QUARANTINE: an autonomous CODE patch whose target has no routed deterministic
        # metric (selfcode.FIT_ROUTES) must NOT auto-ship — the apply gate could only prove it
        # doesn't crash, never that it works. Fall through to the Telegram card below so a human
        # decides. Pure deletions are exempt (the standing measured lesson: structural deletions
        # are the proven-helpful shape, and the crash gate still covers them at apply time).
        quarantined = False
        if self.autonomous and patches and kind == "code":
            try:
                from . import selfcode
                quarantined = selfcode.needs_human(patches)
            except Exception:
                quarantined = True                           # fail CLOSED — never auto-ship unrouted code
        # AUTONOMOUS: just DO it and TEXT Brady — no ✅/❌ question. The money gate is still enforced by the
        # apply Action (gate + locked rails + money-token guard); this only removes the human-approval step.
        if self.autonomous and patches and not quarantined:
            res = self._apply(item)
            item["decision"] = "AUTO-APPLIED"
            item["decided_ts"] = iso(time.time())
            self.st["log"] = (self.st["log"] + [item])[-200:]
            # SCOREBOARD: stamp the loop's vitals at apply time. The coach re-measures ~24h later and
            # reports whether this change actually MOVED the metrics — "survived the gate" is not the
            # reward; "improved the recursive loop" is. (know['self_changes'], evaluated by coach.)
            m0 = self.know.get("model", {})
            ex0 = self.know.get("explore", {})
            tr0 = int(ex0.get("trades", 0) or 0)
            at = {"n": int(m0.get("n", 0) or 0), "brier": float(m0.get("brier", 0.25) or 0.25),
                  "explore": tr0, "dir_hits": int(ex0.get("dir_hits", 0) or 0)}
            # W3.1: a CODE change also gets a deterministic pre-patch baseline — the ledger-replay
            # Brier (selfcode._fitness). The coach scores it on the post-deploy delta of that SAME
            # replay, so the confounded 24h wall-clock diff stops deciding code verdicts.
            if kind == "code":
                try:
                    from . import selfcode
                    fit = selfcode._fitness()
                    if fit:
                        at["fitness"] = fit["brier"]
                except Exception:
                    pass
            self.know.setdefault("self_changes", []).append({
                "id": cid, "ts": time.time(), "title": title[:120], "kind": kind,
                # W3.1 ATTRIBUTION: structured fields the coach reads directly — the old rows carried
                # only a title, so verdicts had to regex-guess WHAT was even changed.
                "target_file": next((str(pp.get("file")) for pp in (patches or []) if pp.get("file")), None),
                "knob": knob if kind == "config" else None,
                "patch_hash": hashlib.sha1(json.dumps(patches, sort_keys=True).encode()).hexdigest()[:12],
                "channel": "live",                          # W3.4b: vs "ci" for ledger-ingested rows
                "at": at})
            self.know["self_changes"] = self.know["self_changes"][-60:]
            m = item.get("meta") or {}
            notify.send(f"🔧 <b>I changed myself</b>\n\n{notify.esc(m.get('plain') or title)}\n\n"
                        f"<b>Why:</b> {notify.esc(m.get('why') or summary)}\n<i>{notify.esc(str(res))}</i>")
            self.persist()
            return cid
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

    def tool_request(self, title: str, why: str) -> bool:
        """The Hive asks Brady (plain English) for a NEW model / data-source / tool it wants. Informational
        — Brady provides the key/wiring. Deduped + capped per day so it can never spam."""
        if not (self.on and notify.enabled()):
            return False
        h = _hash("tool", title, None)
        seen = self.st.setdefault("tool_seen", {})
        if h in seen:
            return False
        day = time.strftime("%Y-%m-%d")
        if self.st.get("tool_day") != day:
            self.st["tool_day"], self.st["tool_count"] = day, 0
        cap = int(self.cfg.get("telegram", {}).get("max_tool_requests_per_day", 3))
        if self.st.get("tool_count", 0) >= cap:
            return False
        seen[h] = 1
        self.st["tool_count"] = self.st.get("tool_count", 0) + 1
        notify.send(f"🧰 <b>I'd like a new tool</b>\n\n{notify.esc(title)}\n\n"
                    f"<b>Why:</b> {notify.esc(why or 'it should help me improve')}\n\n"
                    f"If you agree, add it and I'll wire it in — no money involved.")
        self.persist()
        return True

    def startup_ping(self, text: str):
        if notify.enabled():
            notify.send("🟢 <b>TENTACLE online</b>\n\n" + notify.esc(text))
