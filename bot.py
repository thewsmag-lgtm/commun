"""Scheduled sync job with encrypted state."""
import datetime as dt
import json
import os
import random
import re
import time

import requests
from cryptography.fernet import Fernet
from nectar import Hive

API = "https://api.hive.blog"
STATE_FILE = "state.enc"


def env(name, default=""):
    return os.environ.get(name) or default


ACCOUNT = env("HIVE_ACCOUNT")
POSTING_KEY = env("HIVE_POSTING_KEY")
APP_NAME = env("APP_NAME") or "hive/1.0"  # shown as "via ..." on frontends
MIN_HP = float(env("MIN_HP", "5000"))
DAILY_LIMIT = int(env("DAILY_LIMIT", "20"))
MAX_PER_RUN = int(env("MAX_PER_RUN", "1"))
UPVOTE_WEIGHT = max(1, min(10000, int(env("UPVOTE_WEIGHT", "10000"))))
RUN_EVERY_MIN = int(env("RUN_EVERY_MIN", "30"))
MIN_GAP_MIN = int(env("MIN_GAP_MIN", "25"))
JITTER_MAX_SEC = int(env("JITTER_MAX_SEC", "300"))
COOLDOWN_DAYS = float(env("COOLDOWN_DAYS", "1"))
MODEL = env("MODEL", "gemini-3.5-flash")
LLM_KEY = env("GEMINI_API_KEY")
LLM_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
DRY_RUN = env("DRY_RUN", "true").lower() != "false"
LOG_TEXT = env("LOG_TEXT", "false").lower() == "true"
BLACKLIST = {a.strip().lower() for a in env("BLACKLIST").split(",") if a.strip()}
BLOCKED_COMMUNITIES = {c.strip().lstrip("@").lower() for c in env("BLOCKED_COMMUNITIES").split(",") if c.strip()}
MAX_PAGES = int(env("MAX_PAGES", "100"))
GEMINI_DAILY_CAP = int(env("GEMINI_DAILY_CAP", "18"))  # free tier: 20 requests/day
GEMINI_PER_RUN = int(env("GEMINI_PER_RUN", "3"))
GEMINI_GAP_SEC = int(env("GEMINI_GAP_SEC", "13"))
MIN_AGE_MIN = int(env("MIN_AGE_MIN", "30"))
MAX_AGE_HOURS = float(env("MAX_AGE_HOURS", "24"))
MAX_HP_CHECKS = 150
CHECK_AFTER_HOURS = 6

SYSTEM = env("COMMENT_RULES") or "Write one short, relevant comment for this blog post. Reply with exactly SKIP if you cannot."


def rpc(method, params):
    # Retry temporary Hive API failures (e.g. rate limits or gateway errors).
    last_error = None
    for attempt in range(3):
        try:
            r = requests.post(API, json={"jsonrpc": "2.0", "method": method,
                                         "params": params, "id": 1}, timeout=30)
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(j["error"])
            return j["result"]
        except requests.exceptions.HTTPError as e:
            last_error = e
            status = e.response.status_code if e.response is not None else "unknown"
            print(f"Hive API HTTP {status} on {method} (attempt {attempt + 1}/3)")
            if status not in (429, 500, 502, 503, 504):
                raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            print(f"Hive API connection issue on {method} (attempt {attempt + 1}/3)")
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise last_error


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


# ---------- encrypted state ----------
def load_state():
    f = Fernet(env("STATE_KEY").encode())
    base = {"priority": {}, "tracked": [], "sent": {}, "last_comment": {}, "recent": []}
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
    cutoff = now() - dt.timedelta(hours=MAX_AGE_HOURS)
    out, start = [], {}
    for _ in range(MAX_PAGES):
        try:
            res = rpc("condenser_api.get_discussions_by_created",
                      [{"tag": "", "limit": 20, **start}])
        except Exception as e:
            print("fetch stopped:", type(e).__name__)
            break
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
_acc = {}


def account(author):
    if author not in _acc:
        _acc[author] = rpc("condenser_api.get_accounts", [[author]])[0]
    return _acc[author]


def hp_of(author):
    global _ratio
    if _ratio is None:
        g = rpc("condenser_api.get_dynamic_global_properties", [])
        _ratio = (float(g["total_vesting_fund_hive"].split()[0])
                  / float(g["total_vesting_shares"].split()[0]))
    a = account(author)
    v = lambda k: float(a[k].split()[0])
    vests = v("vesting_shares") + v("received_vesting_shares") - v("delegated_vesting_shares")
    return vests * _ratio


# ---------- skip curation / project / automated accounts ----------
SUBSTR_HINTS = ["curat", "curacion", "kurasyon"] + [
    h.strip().lower() for h in env("EXTRA_SKIP_HINTS").split(",") if h.strip()]
TOKEN_HINTS = {"bot", "news", "official", "project", "community", "daily", "team",
               "pool", "market", "trail", "witness", "dao", "app"}
PROFILE_HINTS = ("curation", "curator", "official account", "project account",
                 "community account", "automated", "this is a bot", "bot account",
                 "curate")
MAX_POSTS_PER_DAY = int(env("MAX_POSTS_PER_DAY", "4"))
MAX_WORDS = int(env("MAX_WORDS", "35"))


def name_looks_like_project(author):
    low = author.lower()
    if re.fullmatch(r"hive-\d+", low):
        return True
    if any(h in low for h in SUBSTR_HINTS):
        return True
    return any(t in TOKEN_HINTS for t in re.split(r"[.\-_0-9]+", low))


def profile_looks_like_project(author):
    try:
        meta = json.loads(account(author).get("posting_json_metadata") or "{}")
    except ValueError:
        return False
    prof = meta.get("profile", {}) if isinstance(meta, dict) else {}
    text = " ".join(str(prof.get(k, "")) for k in ("name", "about")).lower()
    return any(h in text for h in PROFILE_HINTS)


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


DEFAULT_GENERIC = ("great post,truly great,amazing post,awesome post,thanks for sharing,"
                   "well written,keep up the good work,keep it up,nice post,great content")
GENERIC = [g.strip().lower() for g in (env("GENERIC_PHRASES") or DEFAULT_GENERIC).split(",") if g.strip()]


VISUAL = ("image", "photo", "picture", "video", "screenshot", "footage", " pic ", "pics",
          "looked ", "looks like", "looking so", "the view")


def is_generic(text, post):
    low = text.lower()
    if any(v in low + " " for v in VISUAL):
        return True  # the model only reads text, so it must not describe visuals
    if any(g in low for g in GENERIC):
        return True
    body = (post["title"] + " " + post["body"]).lower()
    words = {w for w in re.findall(r"[a-z]{6,}", low)}
    return not any(w in body for w in words)  # must reference something from the post


def too_similar(text, recent):
    words = set(re.findall(r"[a-z']+", text.lower()))
    for old in recent:
        o = set(re.findall(r"[a-z']+", old.lower()))
        if words and o and len(words & o) / len(words | o) > 0.6:
            return True
    return False


def llm_post(**kw):
    """POST to Gemini, retrying only on temporary server errors (a 429 is not retried)."""
    for i in range(3):
        r = requests.post(LLM_URL, **kw)
        if r.status_code in (500, 502, 503, 504) and i < 2:
            time.sleep(20 * (i + 1) + random.randint(0, 5))
            continue
        r.raise_for_status()
        return r


def make_comment(post):
    r = llm_post(
        headers={"x-goog-api-key": LLM_KEY, "Content-Type": "application/json"},
        json={"systemInstruction": {"parts": [{"text": SYSTEM}]},
              "contents": [{"role": "user", "parts": [
                  {"text": f"Title: {post['title']}\n\n{post['body'][:6000]}"}]}],
              "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.8}},
        timeout=60)
    cand = r.json()["candidates"][0]
    if cand.get("finishReason") not in (None, "STOP"):
        return None  # cut off or blocked, never post a partial comment
    parts = cand["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts).strip()
    text = text.replace("\u2014", ",").replace("\u2013", ",")
    text = re.sub(r"@(?=[A-Za-z0-9])", "", text)  # no @mentions
    if text.upper().startswith("SKIP") or len(text) < 15 or len(text.split()) > MAX_WORDS:
        return None
    return text


def main():
    fernet, state = load_state()
    update_priority(state)

    today = now().strftime("%Y-%m-%d")
    sent_today = state["sent"].get(today, 0)
    state["sent"] = {today: sent_today}
    gem_today = state.setdefault("gem", {}).get(today, 0)
    state["gem"] = {today: gem_today}
    run_calls = 0
    budget = min(MAX_PER_RUN, DAILY_LIMIT - sent_today)
    if budget <= 0:
        print("daily limit reached")
        save_state(fernet, state)
        return

    if not DRY_RUN:
        # spread the daily quota across the day instead of posting in bursts
        t = now()
        midnight = (t + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        slots_left = max(1, int((midnight - t).total_seconds() // (RUN_EVERY_MIN * 60)))
        p_run = min(1.0, (DAILY_LIMIT - sent_today) / slots_left)
        too_soon = t.timestamp() - state.get("last_any", 0) < MIN_GAP_MIN * 60
        if too_soon or random.random() > p_run:
            print("skipping this run")
            save_state(fernet, state)
            return
        time.sleep(random.randint(0, JITTER_MAX_SEC))

    posts = recent_posts()
    if BLOCKED_COMMUNITIES:
        n_blocked = sum(1 for p in posts if p.get("category", "").lower() in BLOCKED_COMMUNITIES)
        print(f"posts in blocked communities (skipped): {n_blocked}")
    already = {t["permlink"] for t in state["tracked"]}
    cool = COOLDOWN_DAYS * 86400
    cands = [p for p in posts
             if p["author"].lower() not in BLACKLIST
             and p.get("category", "").lower() not in BLOCKED_COMMUNITIES
             and p["author"] != ACCOUNT
             and now().timestamp() - state["last_comment"].get(p["author"], 0) > cool
             and len(p["body"]) > 400
             and (now() - parse_ts(p["created"])).total_seconds() >= MIN_AGE_MIN * 60]
    counts = {}
    for p in posts:
        counts[p["author"]] = counts.get(p["author"], 0) + 1
    cands = [p for p in cands
             if counts[p["author"]] < MAX_POSTS_PER_DAY  # heavy posters are usually projects
             and not name_looks_like_project(p["author"])]
    random.shuffle(cands)
    cands.sort(key=lambda p: p["author"] not in state["priority"])  # priority first
    print(f"posts={len(posts)} candidates={len(cands)} budget={budget}")

    hive = None if DRY_RUN else Hive(node=[API], keys=[POSTING_KEY])
    checks, done, seen_authors, fails = 0, 0, set(), 0

    for p in cands:
        if done >= budget or checks >= MAX_HP_CHECKS:
            break
        if p["author"] in seen_authors:
            continue
        seen_authors.add(p["author"])
        checks += 1
        try:
            if hp_of(p["author"]) < MIN_HP or profile_looks_like_project(p["author"]):
                continue
            if gem_today >= GEMINI_DAILY_CAP or run_calls >= GEMINI_PER_RUN:
                print("gemini call cap reached, stopping this run")
                break
            gem_today += 1
            run_calls += 1
            state["gem"][today] = gem_today
            try:
                text = make_comment(p)
            finally:
                time.sleep(GEMINI_GAP_SEC)  # stay under the per-minute limit
        except Exception as e:
            detail = getattr(getattr(e, "response", None), "status_code", None)
            suffix = f" HTTP {detail}" if detail is not None else f": {e}"
            resp = getattr(e, "response", None)
            src = "gemini" if resp is not None and "generativelanguage" in str(resp.url) else "hive"
            print(f"skip: {type(e).__name__}{suffix} ({src})")
            if detail == 429 and src == "gemini":
                try:
                    print("gemini says:", resp.json()["error"]["message"][:250])
                except Exception:
                    pass
                print("gemini quota hit, stopping this run")
                break
            fails = fails + 1 if detail == 429 else 0
            if fails >= 3:
                print("rate limited, stopping this run")
                break
            continue
        fails = 0
        if not text or is_generic(text, p) or too_similar(text, state["recent"]):
            continue

        if DRY_RUN:
            print(f"[dry run] would upvote ({UPVOTE_WEIGHT / 100:.0f}%) then comment -> "
                  f"{p['author']}/{p['permlink']}\n{text}\n" if LOG_TEXT
                  else f"[dry run] would upvote then comment -> {p['author']}/{p['permlink']}")
        else:
            # Vote on the target post first. If voting fails, do not publish the comment.
            try:
                hive.vote(weight=UPVOTE_WEIGHT / 100,
                          identifier=f"@{p['author']}/{p['permlink']}",
                          account=ACCOUNT)
            except Exception as e:
                print(f"upvote failed; comment skipped: {type(e).__name__}: {e}")
                continue

            permlink = re.sub(r"[^a-z0-9-]", "-", f"re-{p['author']}-{int(time.time())}".lower())
            try:
                hive.post(title="", body=text, author=ACCOUNT, permlink=permlink,
                          reply_identifier=f"{p['author']}/{p['permlink']}", app=APP_NAME)
            except Exception as e:
                print("post failed after upvote:", type(e).__name__)
                continue
            state["tracked"].append({"target": p["author"], "permlink": permlink,
                                     "ts": now().timestamp(), "checked": False})
            state["last_comment"][p["author"]] = now().timestamp()
            state["last_any"] = now().timestamp()
            state["sent"][today] += 1
            state["recent"] = (state["recent"] + [text])[-30:]
            time.sleep(random.randint(20, 90))
        done += 1

    print(f"commented={done}")
    save_state(fernet, state)


if __name__ == "__main__":
    main()
