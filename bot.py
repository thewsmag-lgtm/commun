"""Scheduled sync job with encrypted state."""
import datetime as dt
import json
import os
import random
import re
import time

import anthropic
import requests
from cryptography.fernet import Fernet
from nectar import Hive

API = "https://api.hive.blog"
STATE_FILE = "state.enc"


def env(name, default=""):
    return os.environ.get(name) or default


ACCOUNT = env("HIVE_ACCOUNT")
POSTING_KEY = env("HIVE_POSTING_KEY")
MIN_HP = float(env("MIN_HP", "5000"))
DAILY_LIMIT = int(env("DAILY_LIMIT", "20"))
MAX_PER_RUN = int(env("MAX_PER_RUN", "4"))
COOLDOWN_DAYS = float(env("COOLDOWN_DAYS", "1"))
MODEL = env("CLAUDE_MODEL", "claude-sonnet-5-5")
DRY_RUN = env("DRY_RUN", "true").lower() != "false"
LOG_TEXT = env("LOG_TEXT", "false").lower() == "true"
BLACKLIST = {a.strip().lower() for a in env("BLACKLIST").split(",") if a.strip()}
MAX_PAGES = 30
MAX_HP_CHECKS = 150
CHECK_AFTER_HOURS = 6

SYSTEM = """You write short blog comments as a friendly reader.
Rules:
- English, warm and conversational, 2 to 3 sentences at most.
- Speak directly to the author and mention something specific from the post.
- Do not use long dashes and do not use the Oxford comma.
- No links, no promotion, no asking for votes, no generic praise like "great post".
- Reply with exactly SKIP if the post is mostly images, a link dump, spam,
  or you cannot say something honest and relevant about it.
Return only the comment text."""


def rpc(method, params):
    r = requests.post(API, json={"jsonrpc": "2.0", "method": method,
                                 "params": params, "id": 1}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


# ---------- encrypted state ----------
def load_state():
    f = Fernet(env("STATE_KEY").encode())
    base = {"priority": {}, "tracked": [], "sent": {}, "last_comment": {}}
    if not os.path.exists(STATE_FILE):
        return f, base
    with open(STATE_FILE, "rb") as fh:
        base.update(json.loads(f.decrypt(fh.read())))
    return f, base


def save_state(f, state):
    with open(STATE_FILE, "wb") as fh:
        fh.write(f.encrypt(json.dumps(state).encode()))


# ---------- hive helpers ----------
def recent_posts():
    cutoff = now() - dt.timedelta(hours=24)
    out, start = [], {}
    for _ in range(MAX_PAGES):
        res = rpc("condenser_api.get_discussions_by_created",
                  [{"tag": "", "limit": 100, **start}])
        if start:
            res = res[1:]
        if not res:
            break
        for p in res:
            if parse_ts(p["created"]) < cutoff:
                return out
            if p["parent_author"] == "":
                out.append(p)
        last = res[-1]
        start = {"start_author": last["author"], "start_permlink": last["permlink"]}
    return out


_ratio = None


def hp_of(author):
    global _ratio
    if _ratio is None:
        g = rpc("condenser_api.get_dynamic_global_properties", [])
        _ratio = (float(g["total_vesting_fund_hive"].split()[0])
                  / float(g["total_vesting_shares"].split()[0]))
    a = rpc("condenser_api.get_accounts", [[author]])[0]
    v = lambda k: float(a[k].split()[0])
    vests = v("vesting_shares") + v("received_vesting_shares") - v("delegated_vesting_shares")
    return vests * _ratio


def update_priority(state):
    """If the post author upvoted our comment, put them on the priority list."""
    t_now = now().timestamp()
    for t in state["tracked"]:
        if t.get("checked"):
            continue
        age_h = (t_now - t["ts"]) / 3600
        if age_h < CHECK_AFTER_HOURS:
            continue
        if age_h > 24 * 7:
            t["checked"] = True
            continue
        try:
            c = rpc("condenser_api.get_content", [ACCOUNT, t["permlink"]])
        except Exception:
            continue
        if t["target"] in [v["voter"] for v in c.get("active_votes", [])]:
            state["priority"][t["target"]] = t_now
            t["checked"] = True
    state["tracked"] = [t for t in state["tracked"]
                        if not t.get("checked") or t_now - t["ts"] < 24 * 3600 * 30]


def make_comment(client, post):
    msg = client.messages.create(
        model=MODEL, max_tokens=200, system=SYSTEM,
        messages=[{"role": "user",
                   "content": f"Title: {post['title']}\n\n{post['body'][:6000]}"}])
    text = msg.content[0].text.strip()
    text = text.replace("\u2014", ",").replace("\u2013", ",")
    return None if text.upper().startswith("SKIP") or len(text) < 20 else text


def main():
    fernet, state = load_state()
    update_priority(state)

    today = now().strftime("%Y-%m-%d")
    sent_today = state["sent"].get(today, 0)
    state["sent"] = {today: sent_today}
    budget = min(MAX_PER_RUN, DAILY_LIMIT - sent_today)
    if budget <= 0:
        print("daily limit reached")
        save_state(fernet, state)
        return

    posts = recent_posts()
    already = {t["permlink"] for t in state["tracked"]}
    cool = COOLDOWN_DAYS * 86400
    cands = [p for p in posts
             if p["author"].lower() not in BLACKLIST
             and p["author"] != ACCOUNT
             and now().timestamp() - state["last_comment"].get(p["author"], 0) > cool
             and len(p["body"]) > 400]
    random.shuffle(cands)
    cands.sort(key=lambda p: p["author"] not in state["priority"])  # priority first
    print(f"posts={len(posts)} candidates={len(cands)} budget={budget}")

    client = anthropic.Anthropic()
    hive = None if DRY_RUN else Hive(node=[API], keys=[POSTING_KEY])
    checks, done, seen_authors = 0, 0, set()

    for p in cands:
        if done >= budget or checks >= MAX_HP_CHECKS:
            break
        if p["author"] in seen_authors:
            continue
        seen_authors.add(p["author"])
        checks += 1
        try:
            if hp_of(p["author"]) < MIN_HP:
                continue
            text = make_comment(client, p)
        except Exception as e:
            print("skip:", type(e).__name__)
            continue
        if not text:
            continue

        if DRY_RUN:
            print(f"[dry run] -> {p['author']}/{p['permlink']}\n{text}\n" if LOG_TEXT
                  else "[dry run] comment generated")
        else:
            permlink = re.sub(r"[^a-z0-9-]", "-", f"re-{p['author']}-{int(time.time())}".lower())
            try:
                hive.post(title="", body=text, author=ACCOUNT, permlink=permlink,
                          reply_identifier=f"{p['author']}/{p['permlink']}")
            except Exception as e:
                print("post failed:", type(e).__name__)
                continue
            state["tracked"].append({"target": p["author"], "permlink": permlink,
                                     "ts": now().timestamp(), "checked": False})
            state["last_comment"][p["author"]] = now().timestamp()
            state["sent"][today] += 1
            time.sleep(random.randint(20, 90))
        done += 1

    print(f"commented={done}")
    save_state(fernet, state)


if __name__ == "__main__":
    main()
