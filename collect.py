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

LOOKBACK = 7            # 좋아요·댓글(참여) 집계 기간(일)
PAGE_SIZE = 200        # 한 요청에 많이 받아 릴스가 뒤로 밀린 계정도 한 번에 잡음(요청 수↓ → 레이트리밋에 유리)
MAX_PAGES = 6          # 채널당 최대 페이지(무한 페이지네이션 방지)
OUT_PATH = "docs/data.json"
HISTORY_KEEP = 60
KST = dt.timezone(dt.timedelta(hours=9))

# ── 릴스 조회수(효율) 설정 ──────────────────────────────
REELS_N = 20           # 최근 업로드순 릴스 몇 개로 중앙값을 낼지
MIN_REELS = 1          # 릴스가 이 개수 이상이면 조회수 중앙값 산출(1개면 그 값 그대로)
FRESH_HOURS = 24       # 업로드 후 이 시간 안 지난 릴스는 제외(조회수 덜 쌓임)

# view_count: 릴스에만 값이 옴 / media_product_type: REELS 판별용
MEDIA_FIELDS = ("id,media_type,media_product_type,timestamp,permalink,"
                "like_count,comments_count,view_count")

RL_CODES = {4, 17, 32, 613, 368}   # 레이트리밋/차단으로 보는 에러 코드
MAX_RL_STREAK = 5                  # 연속 이만큼 막히면 조기 중단

# HTML(플래너)이 계정 목록을 저장하는 Supabase — index.html과 동일 값
SUPA_URL = "https://nmsrzdlgmaaqqcalfbhr.supabase.co"
SUPA_KEY = "sb_publishable_ZWCmMNsV1La-yMawjhzUtg_FWQp7-O1"

# 계정별로 '어떤 지표가 필요한가'를 load_targets에서 채운다.
#   VIDEO_ACCOUNTS : 릴스 조회수(효율)가 필요한 계정 (type=='video')
#   ENGAGE_ACCOUNTS: 좋아요·댓글(참여) 효율이 필요한 계정 (type!='video')
# 같은 계정이 영상+이미지(또는 영상+텍스트) 겸용이면 두 집합에 모두 들어가,
# 한 번의 수집에서 조회수와 좋아요를 같이 받는다(겸용 계정 좋아요 0 문제 해결).
VIDEO_ACCOUNTS = set()
ENGAGE_ACCOUNTS = set()


def log(msg):
    print(msg, flush=True)   # Actions에서 진행 상황이 바로 보이도록 즉시 출력


def median(xs):
    """정수로 반올림한 중앙값. 빈 리스트면 None."""
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    m = n // 2
    if n % 2:
        return xs[m]
    return round((xs[m - 1] + xs[m]) / 2)


def load_targets():
    """수집 대상 목록 = HTML에서 추가한 계정(Supabase) + channels.txt (중복 제거).
    - HTML에서 계정을 추가하면 다음 수집부터 자동 포함됨(깃허브 수동 편집 불필요).
    - channels.txt는 수동 보조 목록 겸, Supabase가 안 될 때의 폴백으로 유지.
    """
    names, seen = [], set()

    def add(n):
        n = (n or "").strip().lstrip("@")
        if n and n not in seen:
            seen.add(n)
            names.append(n)

    # 1) HTML에서 추가한 계정 (Supabase store.accounts)
    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/store",
            params={"k": "eq.accounts", "select": "v"},
            headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"},
            timeout=20,
        )
        r.raise_for_status()
        rows = r.json()
        if rows and isinstance(rows[0].get("v"), list):
            for a in rows[0]["v"]:
                add(a.get("name"))
                nm = (a.get("name") or "").strip().lstrip("@")
                if nm:
                    if a.get("type") == "video":
                        VIDEO_ACCOUNTS.add(nm)
                    else:
                        ENGAGE_ACCOUNTS.add(nm)   # 이미지·텍스트 등 영상 외 유형
        both = VIDEO_ACCOUNTS & ENGAGE_ACCOUNTS
        log(f"Supabase 계정 {len(names)}개 로드 "
            f"(영상 {len(VIDEO_ACCOUNTS)} · 참여 {len(ENGAGE_ACCOUNTS)} · 겸용 {len(both)})")
    except Exception as e:
        log(f"Supabase 로드 실패 → channels.txt로 대체: {e}")

    # 2) channels.txt (수동 보조 목록 / 폴백) — '#'는 주석
    try:
        with open("channels.txt", encoding="utf-8") as f:
            for ln in f:
                ln = ln.split("#", 1)[0].strip()
                if ln:
                    add(ln)
    except FileNotFoundError:
        pass

    log(f"수집 대상 총 {len(names)}개")
    return names


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
    """네트워크/일시 오류만 짧게 재시도(3초). 레이트리밋(앱 요청 한도)은
    몇 초 기다려도 안 풀리고 재시도가 한도만 더 깎으므로 즉시 반환한다."""
    url = f"https://graph.facebook.com/{GRAPH_VER}/{IG_USER_ID}"
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(3)          # 통신 끊김 등 일시 오류는 재시도
            continue
        if r.status_code == 200:
            return r.json()
        err = {}
        try:
            err = r.json().get("error", {})
        except Exception:
            err = {"message": r.text}
        # 레이트리밋이면 재시도 없이 바로 반환(헛호출로 한도 소모 방지).
        # 그 외 에러도 즉시 반환.
        return {"error": err or {"message": "unknown"}}
    return {"error": {"message": "max retries exceeded", "code": 4}}


def fetch(username, since, now, want_reels=False, want_eng=True):
    items, reels, followers, after, pages = [], [], None, None, 0
    engagement_done = False
    fresh_cut = now - dt.timedelta(hours=FRESH_HOURS)
    # 진단용 카운터 (영상 계정이 왜 0인지 추적)
    diag = {"rows": 0, "video": 0, "reels_tagged": 0, "reels_withview": 0, "pages": 0}
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
        old_on_page = 0
        diag["rows"] += len(rows)
        for it in rows:
            ts = parse_ts(it.get("timestamp"))
            if not ts:
                continue
            # 겸용 계정은 두 블록이 모두 돌아 조회수와 좋아요를 같은 응답에서 함께 수집.
            if want_reels:
                # 릴스 조회수(효율). 핀이 날짜순서를 흐트러도 나중에 timestamp로
                # 정렬해 최근 N개만 쓰므로 오래된 핀은 자동 제외.
                if it.get("media_type") == "VIDEO":
                    diag["video"] += 1
                if it.get("media_product_type") == "REELS":
                    diag["reels_tagged"] += 1
                if it.get("media_product_type") == "REELS" and it.get("view_count") is not None:
                    diag["reels_withview"] += 1
                    reels.append({"id": it.get("id"), "_ts": ts,
                                  "views": it.get("view_count") or 0,
                                  "permalink": it.get("permalink", "")})
            if want_eng:
                # 참여(좋아요·댓글): 집계 기간(LOOKBACK*2) 내 '피드 게시물'만.
                # 릴스(REELS)는 영상 지표(조회수)로 따로 집계하므로 좋아요 효율에서 제외.
                if ts < since:
                    old_on_page += 1          # 과거 구간 도달 판단은 전체 미디어 기준(안정적)
                elif it.get("media_product_type") != "REELS":
                    it["_ts"] = ts
                    items.append(it)
        # 핀은 최대 3개. 한 페이지에서 '오래된 글'이 3개 넘으면 진짜 과거 구간 도달.
        if old_on_page > 3:
            engagement_done = True
        reels_old = sum(1 for r in reels if r["_ts"] <= fresh_cut)
        paging = media.get("paging", {})
        after = paging.get("cursors", {}).get("after")
        # 필요한 지표를 모두 확보하면 종료:
        #   릴스 → 24h 지난 릴스 N개 확보 / 참여 → 집계 구간 덮음
        # 겸용이면 둘 다 만족해야 멈춘다(한쪽만 끝나고 멈춰 다른 쪽이 0 되는 것 방지).
        reels_done = (not want_reels) or (reels_old >= REELS_N)
        eng_done = (not want_eng) or engagement_done
        if (reels_done and eng_done) or not paging.get("next") or not after:
            break
    diag["pages"] = pages
    return {"followers": followers, "items": items, "reels": reels, "diag": diag}, None


def summarize(username, followers, items, reels, now):
    rs = now - dt.timedelta(days=LOOKBACK)
    ps = now - dt.timedelta(days=LOOKBACK * 2)
    recent = [it for it in items if it["_ts"] >= rs]
    prior = [it for it in items if ps <= it["_ts"] < rs]

    # ── 릴스 조회수 중앙값(효율) ──
    # 1) id 중복 제거  2) 업로드 24h 이내 제외  3) 최신순 N개  4) 중앙값
    fresh_cut = now - dt.timedelta(hours=FRESH_HOURS)
    seen, uniq = set(), []
    for r in sorted(reels, key=lambda r: r["_ts"], reverse=True):
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    eligible = [r for r in uniq if r["_ts"] <= fresh_cut]   # 이미 최신순
    used = eligible[:REELS_N]
    view_median = median([r["views"] for r in used]) if len(used) >= MIN_REELS else None
    reels_used = len(used) if view_median is not None else 0
    top_reels = ([{"views": r["views"],
                   "date": r["_ts"].astimezone(KST).strftime("%Y-%m-%d"),
                   "permalink": r["permalink"]}
                  for r in sorted(used, key=lambda r: r["views"], reverse=True)[:5]]
                 if view_median is not None else [])

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
        "view_median": view_median,
        "reels_used": reels_used,
        "top_reels": top_reels,
        "error": None,
    }


# ── 부분 갱신(merge) 설정 ───────────────────────────────
# 시간대 분할 수집에서 '담당 필드만' 덮어쓰기 위한 분류.
#   engage 런(참여) → ENG_FIELDS 만 갱신
#   video  런(조회수) → VID_FIELDS 만 갱신
# 이렇게 해야 5시(좋아요)·8시(조회수) 결과가 서로를 덮어쓰지 않음.
ENG_FIELDS = ("posts_7d", "avg_likes", "avg_comments", "avg_engagement",
              "eng_rate", "trend_pct", "posts")
VID_FIELDS = ("view_median", "reels_used", "top_reels")


def merge_into(base, s, mode):
    """기존 채널 엔트리(base) 위에 이번 수집 결과(s)의 '담당 필드'만 덮어쓴다.
    base가 없으면 새로 시작. 담당 아닌 필드는 base 값 그대로 보존."""
    out = dict(base) if isinstance(base, dict) else {}
    out["username"] = s["username"]
    out["error"] = None
    # 팔로워 수는 두 런 모두 받을 수 있음 → 받은 쪽이 최신값으로 갱신
    if s.get("followers") is not None:
        out["followers"] = s["followers"]
    elif "followers" not in out:
        out["followers"] = None
    if mode in ("engage", "all"):
        for k in ENG_FIELDS:
            out[k] = s.get(k)
    if mode in ("video", "all"):
        for k in VID_FIELDS:
            out[k] = s.get(k)
    return out


def main():
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=LOOKBACK * 2)
    today = dt.datetime.now(KST).strftime("%Y-%m-%d")

    # 수집 모드: engage(참여/5시) · video(조회수/8시) · all(전부/기본)
    mode = os.environ.get("COLLECT_MODE", "all").strip().lower()
    if mode not in ("engage", "video", "all"):
        log(f"알 수 없는 COLLECT_MODE='{mode}' → 'all'로 진행")
        mode = "all"
    log(f"수집 모드: {mode}")

    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        store = {}
    history = store.get("history", {})
    # 기존 data.json의 모든 채널을 username으로 인덱싱 → 보존/merge의 기준(baseline)
    prev_all = {c.get("username"): c for c in store.get("channels", [])
                if isinstance(c, dict) and c.get("username")}

    all_targets = load_targets()
    # 모드별 대상: engage=참여 필요 계정 / video=영상 계정 (겸용은 양쪽 모두 포함)
    if mode == "engage":
        targets = [u for u in all_targets
                   if (u in ENGAGE_ACCOUNTS) or (u not in VIDEO_ACCOUNTS)]
    elif mode == "video":
        targets = [u for u in all_targets if u in VIDEO_ACCOUNTS]
    else:
        targets = all_targets
    total = len(targets)
    log(f"모드 '{mode}' 대상 {total}개 (전체 {len(all_targets)}개)")

    # 최종 출력 = 기존 전체를 깔고, 이번 런이 담당분만 갱신(나머지 계정·필드 보존)
    result = {u: c for u, c in prev_all.items()}

    ok_count = 0
    rl_streak = 0
    aborted = False

    for idx, u in enumerate(targets, 1):
        if mode == "engage":
            want_reels, want_eng = False, True
        elif mode == "video":
            want_reels, want_eng = True, False
        else:  # all
            want_reels = u in VIDEO_ACCOUNTS
            want_eng = (u in ENGAGE_ACCOUNTS) or (u not in VIDEO_ACCOUNTS)
        tag = " (겸용)" if (want_reels and want_eng) else (" (영상)" if want_reels else "")
        log(f"[{idx}/{total}] @{u}{tag} ...")
        res, err = fetch(u, since, now, want_reels, want_eng)
        if err:
            rl = is_rate_limit(err)
            rl_streak = rl_streak + 1 if rl else 0
            msg = err.get("message", "error") if isinstance(err, dict) else str(err)
            if u in result:
                log(f"    ⚠️ 실패({msg}) → 직전 데이터 유지")
            else:
                result[u] = {"username": u, "error": msg}
                log(f"    ⚠️ 실패: {msg}")
            if rl_streak >= MAX_RL_STREAK:
                log(f"연속 {rl_streak}회 레이트리밋 → 앱이 막힌 상태로 보여 수집 중단합니다.")
                for v in targets[idx:]:   # 남은 대상: baseline 있으면 보존, 없으면 skipped
                    if v not in result:
                        result[v] = {"username": v, "error": "skipped (rate limited)"}
                aborted = True
                break
            time.sleep(3)
            continue

        rl_streak = 0
        s = summarize(u, res["followers"], res["items"], res["reels"], now)
        result[u] = merge_into(result.get(u), s, mode)
        ok_count += 1

        # history: 그날 포인트에 담당 지표만 갱신(다른 지표는 기존 값 보존)
        day = next((dict(p) for p in history.get(u, []) if p.get("date") == today), {})
        day["date"] = today
        if mode in ("engage", "all"):
            day["eng_rate"] = s["eng_rate"]
            day["avg_engagement"] = s["avg_engagement"]
        if mode in ("video", "all"):
            day["view_median"] = s["view_median"]
        pts = [p for p in history.get(u, []) if p.get("date") != today]
        pts.append(day)
        history[u] = pts[-HISTORY_KEEP:]

        if want_reels:
            dg = res.get("diag", {})
            log(f"    [ok] 조회수중앙값 {s['view_median']} (릴스 {s['reels_used']}개 사용) "
                f"| 진단: {dg.get('pages')}페이지·전체{dg.get('rows')}개 중 "
                f"영상{dg.get('video')}·REELS태그{dg.get('reels_tagged')}·조회수有{dg.get('reels_withview')}")
        if want_eng:
            log(f"    [ok] 참여율 {s['eng_rate']}% / 평균좋아요 {s['avg_likes']}")
        time.sleep(3)

    # 안전장치: 이번에 새로 성공한 계정이 0개면 기존 data.json 보존(덮어쓰지 않음)
    if ok_count == 0:
        log("새로 수집된 계정이 0개입니다(레이트리밋/차단 추정). "
            "기존 data.json을 보존하고 덮어쓰지 않습니다. 잠시 후 다시 실행하세요.")
        sys.exit(0)

    channels = list(result.values())
    channels.sort(
        key=lambda c: c.get("eng_rate") if c.get("eng_rate") is not None else -1,
        reverse=True,
    )
    now_kst = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    # 지표별 '마지막 수집 시각' — 담당 모드만 now로 갱신, 나머지는 기존 값 보존.
    # (화면에서 좋아요·조회수가 각각 언제 기준인지 구분하기 위함)
    engage_ts = store.get("engage_updated_kst")
    video_ts = store.get("video_updated_kst")
    if mode in ("engage", "all"):
        engage_ts = now_kst
    if mode in ("video", "all"):
        video_ts = now_kst
    out = {
        "updated_at": now.isoformat(),
        "updated_kst": now_kst,          # 마지막 저장 시각(호환용)
        "engage_updated_kst": engage_ts,  # 좋아요·참여 마지막 수집 시각
        "video_updated_kst": video_ts,    # 조회수 마지막 수집 시각
        "lookback_days": LOOKBACK,
        "channels": channels,
        "history": history,
    }
    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log(f"완료[{mode}]: {ok_count}/{total} 신규 수집, data.json 저장"
        + (" (중간 중단됨)" if aborted else ""))


if __name__ == "__main__":
    main()
