#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
협찬 채널 지표 자동 수집 (GitHub Actions용)
- 토큰/계정ID는 환경변수(GitHub Secrets)에서 읽음
- 공개 프로페셔널 채널의 최근 7일 좋아요·댓글 → 평균·참여율·추이 계산
- 결과를 docs/data.json 에 저장 (대시보드가 읽음)
- 개선: 레이트리밋 시 빨리 포기 / 연속 차단 시 조기 중단 /
        전부 실패하면 기존 data.json 보존(대시보드 안 날아감) /
        일시 오류 계정은 직전 데이터 유지 / 진행 로그 즉시 출력
"""

import os
import sys
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
MAX_PAGES = 6          # 채널당 최대 페이지(무한 페이지네이션 방지)
OUT_PATH = "docs/data.json"
HISTORY_KEEP = 60
KST = dt.timezone(dt.timedelta(hours=9))
MEDIA_FIELDS = "id,media_type,timestamp,permalink,like_count,comments_count"

RL_CODES = {4, 17, 32, 613, 368}   # 레이트리밋/차단으로 보는 에러 코드
MAX_RL_STREAK = 5                  # 연속 이만큼 막히면 조기 중단


def log(msg):
    print(msg, flush=True)   # Actions에서 진행 상황이 바로 보이도록 즉시 출력


def load_targets():
    with open("channels.txt", encoding="utf-8") as f:
        return [ln.strip().lstrip("@") for ln in f
                if ln.strip() and not ln.lstrip().startswith("#")]


def parse_ts(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except (ValueError, TypeError):
        return None


def is_rate_limit(err):
    if not isinstance(err, dict):
        return False
    if err.get("code") in RL_CODES:
        return True
    msg = str(err.get("message", "")).lower()
    return ("rate limit" in msg) or ("request limit" in msg) or ("blocked" in msg)


def api_get(params, retries=3):
    """짧은 백오프(3,6초). 레이트리밋이면 재시도, 그 외 에러는 즉시 반환."""
    url = f"https://graph.facebook.com/{GRAPH_VER}/{IG_USER_ID}"
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 200:
            return r.json()
        err = {}
        try:
            err = r.json().get("error", {})
        except Exception:
            err = {"message": r.text}
        if (is_rate_limit(err) or r.status_code == 429) and i < retries - 1:
            time.sleep(3 * (2 ** i))   # 3, 6
            continue
        return {"error": err or {"message": "unknown"}}
    return {"error": {"message": "max retries exceeded", "code": 4}}


def fetch(username, since):
    items, followers, after, pages = [], None, None, 0
    while pages < MAX_PAGES:
        pages += 1
        if after:
            bd = (f"business_discovery.username({username})"
                  f"{{media.after({after}).limit({PAGE_SIZE}){{{MEDIA_FIELDS}}}}}")
        else:
            bd = (f"business_discovery.username({username})"
                  f"{{followers_count,media.limit({PAGE_SIZE}){{{MEDIA_FIELDS}}}}}")
        data = api_get({"fields": bd, "access_token": TOKEN})
        if "error" in data:
            return None, data["error"]
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
    prev_ok = {c.get("username"): c for c in store.get("channels", [])
               if isinstance(c, dict) and not c.get("error") and c.get("followers") is not None}

    targets = load_targets()
    total = len(targets)
    channels = []
    ok_count = 0
    rl_streak = 0
    aborted = False

    for idx, u in enumerate(targets, 1):
        log(f"[{idx}/{total}] @{u} ...")
        res, err = fetch(u, since)
        if err:
            rl = is_rate_limit(err)
            rl_streak = rl_streak + 1 if rl else 0
            msg = err.get("message", "error") if isinstance(err, dict) else str(err)
            if u in prev_ok:
                channels.append(prev_ok[u])
                log(f"    ⚠️ 실패({msg}) → 직전 데이터 유지")
            else:
                channels.append({"username": u, "error": msg})
                log(f"    ⚠️ 실패: {msg}")
            if rl_streak >= MAX_RL_STREAK:
                log(f"연속 {rl_streak}회 레이트리밋 → 앱이 막힌 상태로 보여 수집 중단합니다.")
                for v in targets[idx:]:
                    if v in prev_ok:
                        channels.append(prev_ok[v])
                    else:
                        channels.append({"username": v, "error": "skipped (rate limited)"})
                aborted = True
                break
            time.sleep(0.5)
            continue

        rl_streak = 0
        s = summarize(u, res["followers"], res["items"], now)
        channels.append(s)
        ok_count += 1
        pts = [p for p in history.get(u, []) if p.get("date") != today]
        pts.append({"date": today, "eng_rate": s["eng_rate"],
                    "avg_engagement": s["avg_engagement"]})
        history[u] = pts[-HISTORY_KEEP:]
        log(f"    [ok] 참여율 {s['eng_rate']}% / 평균좋아요 {s['avg_likes']}")
        time.sleep(0.5)

    # 안전장치: 이번에 새로 성공한 계정이 0개면 기존 data.json 보존(덮어쓰지 않음)
    if ok_count == 0:
        log("새로 수집된 계정이 0개입니다(레이트리밋/차단 추정). "
            "기존 data.json을 보존하고 덮어쓰지 않습니다. 잠시 후 다시 실행하세요.")
        sys.exit(0)

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
    log(f"완료: {ok_count}/{total} 신규 수집, data.json 저장"
        + (" (중간 중단됨)" if aborted else ""))


if __name__ == "__main__":
    main()
