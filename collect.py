#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
협찬 채널 지표 자동 수집 (GitHub Actions용)
- 토큰/계정ID는 환경변수(GitHub Secrets)에서 읽음
- 공개 프로페셔널 채널의 최근 7일 좋아요·댓글 → 평균·참여율·추이 계산
- 결과를 docs/data.json 에 저장 (대시보드가 읽음)
- 매일 실행 시 채널별 '일자별 추이'를 history에 누적
"""

import os
import json
import time
import datetime as dt
from statistics import mean

import requests

GRAPH_VER = "v26.0"
TOKEN = os.environ["IG_ACCESS_TOKEN"]
IG_USER_ID = os.environ["IG_USER_ID"]

LOOKBACK = 7            # 집계 기간(일)
PAGE_SIZE = 50
OUT_PATH = "docs/data.json"
HISTORY_KEEP = 60       # 추이 그래프용 최근 N일 보관
KST = dt.timezone(dt.timedelta(hours=9))
MEDIA_FIELDS = "id,media_type,timestamp,permalink,like_count,comments_count"


def load_targets():
    with open("channels.txt", encoding="utf-8") as f:
        return [ln.strip().lstrip("@") for ln in f
                if ln.strip() and not ln.lstrip().startswith("#")]


def parse_ts(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except (ValueError, TypeError):
        return None


def api_get(params, retries=5):
    url = f"https://graph.facebook.com/{GRAPH_VER}/{IG_USER_ID}"
    for i in range(retries):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        err = {}
        try:
            err = r.json().get("error", {})
        except Exception:
            pass
        if err.get("code") in (4, 17, 32, 613) or r.status_code == 429:
            time.sleep(min(90, 5 * (2 ** i)))
            continue
        return {"error": err or {"message": r.text}}
    return {"error": {"message": "max retries exceeded"}}


def fetch(username, since):
    items, followers, after = [], None, None
    while True:
        if after:
            bd = (f"business_discovery.username({username})"
                  f"{{media.after({after}).limit({PAGE_SIZE}){{{MEDIA_FIELDS}}}}}")
        else:
            bd = (f"business_discovery.username({username})"
                  f"{{followers_count,media.limit({PAGE_SIZE}){{{MEDIA_FIELDS}}}}}")
        data = api_get({"fields": bd, "access_token": TOKEN})
        if "error" in data:
            return None, data["error"].get("message", "error")
        bdisc = data.get("business_discovery", {})
        if bdisc.get("followers_count") is not None:
            followers = bdisc["followers_count"]
        media = bdisc.get("media", {})
        rows = media.get("data", [])
        if not rows:
            break
        old = False
        for it in rows:
            ts = parse_ts(it.get("timestamp"))
            if not ts:
                continue
            if ts < since:
                old = True
                continue
            it["_ts"] = ts
            items.append(it)
        paging = media.get("paging", {})
        after = paging.get("cursors", {}).get("after")
        if old or not paging.get("next") or not after:
            break
    return {"followers": followers, "items": items}, None


def summarize(username, followers, items, now):
    rs = now - dt.timedelta(days=LOOKBACK)
    ps = now - dt.timedelta(days=LOOKBACK * 2)
    recent = [it for it in items if it["_ts"] >= rs]
    prior = [it for it in items if ps <= it["_ts"] < rs]

    def eng(it):
        return (it.get("like_count") or 0) + (it.get("comments_count") or 0)

    r_likes = [it.get("like_count") or 0 for it in recent]
    r_comments = [it.get("comments_count") or 0 for it in recent]
    r_eng = [eng(it) for it in recent]
    p_eng = [eng(it) for it in prior]

    avg_eng = mean(r_eng) if r_eng else 0
    prior_eng = mean(p_eng) if p_eng else 0
    trend = round((avg_eng - prior_eng) / prior_eng * 100, 1) if prior_eng > 0 else None
    eng_rate = round(avg_eng / followers * 100, 2) if followers else None

    posts_out = sorted(
        [{"likes": it.get("like_count") or 0,
          "comments": it.get("comments_count") or 0,
          "date": it["_ts"].astimezone(KST).strftime("%Y-%m-%d"),
          "permalink": it.get("permalink", "")} for it in recent],
        key=lambda p: p["date"])[-40:]

    return {
        "username": username,
        "followers": followers,
        "posts_7d": len(recent),
        "avg_likes": round(mean(r_likes)) if r_likes else 0,
        "avg_comments": round(mean(r_comments)) if r_comments else 0,
        "avg_engagement": round(avg_eng),
        "eng_rate": eng_rate,
        "trend_pct": trend,
        "posts": posts_out,
        "error": None,
    }


def main():
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=LOOKBACK * 2)
    today = dt.datetime.now(KST).strftime("%Y-%m-%d")

    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        store = {}
    history = store.get("history", {})

    channels = []
    for u in load_targets():
        res, err = fetch(u, since)
        if err:
            channels.append({"username": u, "error": err})
            print(f"  [!] @{u}: {err}")
            continue
        s = summarize(u, res["followers"], res["items"], now)
        channels.append(s)
        pts = [p for p in history.get(u, []) if p.get("date") != today]
        pts.append({"date": today, "eng_rate": s["eng_rate"],
                    "avg_engagement": s["avg_engagement"]})
        history[u] = pts[-HISTORY_KEEP:]
        print(f"  [ok] @{u}: 참여율 {s['eng_rate']}% / 평균참여 {s['avg_engagement']}")
        time.sleep(1)

    channels.sort(
        key=lambda c: c.get("eng_rate") if c.get("eng_rate") is not None else -1,
        reverse=True,
    )

    out = {
        "updated_at": now.isoformat(),
        "updated_kst": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "lookback_days": LOOKBACK,
        "channels": channels,
        "history": history,
    }
    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote {OUT_PATH} ({len(channels)} channels)")


if __name__ == "__main__":
    main()
