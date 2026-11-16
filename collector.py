"""실시간 지하철·버스 데이터 수집기 (표준 라이브러리만 사용)

작업 스케줄러가 1분마다 `collector.py --tick` 을 실행하면, 지금 시각에 해당하는
작업만 골라 API를 호출하고 data/ 아래에 하루 단위 .jsonl.gz 로 쌓습니다.

    py collector.py --plan     요일별 하루 예상 호출 수 확인 (호출 안 함)
    py collector.py --test     API마다 1번씩 호출해 키·응답 확인
    py collector.py --tick     지금 시각에 해당하는 작업 실행 (스케줄러용)
"""
import argparse
import datetime as dt
import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
KST = dt.timezone(dt.timedelta(hours=9))

# api 이름 -> (일일 한도를 공유하는 키 그룹, .env 변수명)
API_KEYS = {
    "subway_position": ("subway", "SEOUL_SUBWAY_REALTIME_KEY"),
    "subway_arrival": ("subway", "SEOUL_SUBWAY_REALTIME_KEY"),
    "seoul_bus_position": ("seoul_bus", "DATA_GO_KR_KEY"),
    "incheon_bus_position": ("incheon_bus", "DATA_GO_KR_KEY"),
}


def load_env():
    env = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            k, sep, v = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                env[k.strip()] = v.strip()
    return env


def load_config():
    return json.loads((ROOT / "collect_config.json").read_text(encoding="utf-8"))


def log(msg):
    (DATA / "_log").mkdir(parents=True, exist_ok=True)
    line = f"{dt.datetime.now(KST):%Y-%m-%d %H:%M:%S} {msg}"
    with open(DATA / "_log" / "collector.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


# ---------- 스케줄 ----------

def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def day_matches(days, d):
    weekend = d.weekday() >= 5
    return days == "all" or (days == "weekend") == weekend


def due_calls(cfg, now):
    """지금 시각(분 단위)에 호출할 (api, target, job) 목록. 같은 api+target 은 1번만."""
    mod = now.hour * 60 + now.minute
    due = {}
    for job in cfg["jobs"]:
        if not job.get("enabled", True) or not job["targets"]:
            continue
        if not day_matches(job["days"], now.date()):
            continue
        for start, end in job["windows"]:
            s, e = _minutes(start), _minutes(end)
            if s <= mod <= e and (mod - s) % job["interval_min"] == 0:
                for t in job["targets"]:
                    due.setdefault((job["api"], t), job["name"])
                break
    return [(api, t, name) for (api, t), name in due.items()]


def plan(cfg):
    for label, day in (("평일", dt.date(2026, 11, 16)), ("주말", dt.date(2026, 11, 21))):
        counts = {}
        for mod in range(24 * 60):
            now = dt.datetime.combine(day, dt.time(mod // 60, mod % 60), KST)
            for api, _, _ in due_calls(cfg, now):
                g = API_KEYS[api][0]
                counts[g] = counts.get(g, 0) + 1
        caps = cfg["daily_cap"]
        print(f"{label}: " + ", ".join(f"{g} {n}회 (한도 {caps.get(g)})" for g, n in counts.items()))


# ---------- 일일 한도 ----------

def _count_file(day):
    return DATA / "_calls" / f"{day}.json"


def calls_today(day):
    f = _count_file(day)
    return json.loads(f.read_text()) if f.exists() else {}


def add_call(day, group):
    f = _count_file(day)
    f.parent.mkdir(parents=True, exist_ok=True)
    c = calls_today(day)
    c[group] = c.get(group, 0) + 1
    f.write_text(json.dumps(c))


# ---------- API 호출 ----------

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "DataAnalysis2026-collector"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def fetch(api, target, env):
    """(rows, 상태메시지) 반환. rows 는 API 원본 필드를 그대로 담은 dict 리스트."""
    key = env.get(API_KEYS[api][1], "")
    if not key or not key.isascii():
        raise RuntimeError(f".env 의 {API_KEYS[api][1]} 가 비어 있거나 예시 문구입니다")
    q = urllib.parse.quote

    if api in ("subway_position", "subway_arrival"):
        svc, field = {
            "subway_position": ("realtimePosition", "realtimePositionList"),
            "subway_arrival": ("realtimeStationArrival", "realtimeArrivalList"),
        }[api]
        url = f"http://swopenapi.seoul.go.kr/api/subway/{q(key)}/json/{svc}/0/300/{q(target)}"
        d = json.loads(http_get(url))
        err = d.get("errorMessage") or {"code": d.get("code"), "message": d.get("message")}
        return d.get(field) or [], f"{err.get('code')} {err.get('message')}"

    if api == "seoul_bus_position":
        route_id = seoul_route_id(target, key)
        items, status = _seoul_bus_get(
            "http://ws.bus.go.kr/api/rest/buspos/getBusPosByRtid"
            f"?serviceKey={q(key, safe='')}&busRouteId={q(route_id)}&resultType=json")
        return [{"_route_id": route_id, **it} for it in items], status

    if api == "incheon_bus_position":
        rows, status = [], []
        for route_id in incheon_route_ids(target, key):  # 왕복 노선이 여러 ID일 수 있음
            items, st = _incheon_get(
                "http://apis.data.go.kr/6280000/busLocationService/getBusRouteLocation"
                f"?serviceKey={q(key, safe='')}&routeId={q(route_id)}&numOfRows=100&pageNo=1")
            rows += [{"_route_id": route_id, **it} for it in items]
            status.append(st)
        return rows, " / ".join(status)

    raise ValueError(api)


def _incheon_get(url):
    text = http_get(url)
    try:
        d = json.loads(text)
    except json.JSONDecodeError:  # 인천 API는 기본이 XML
        return _xml_items(text)
    body = (d.get("response") or {}).get("body") or {}
    items = (body.get("items") or {}).get("itemList") or (body.get("items") or {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    h = (d.get("response") or {}).get("header") or {}
    return items, f"{h.get('resultCode')} {h.get('resultMsg')}"


def _seoul_bus_get(url):
    d = json.loads(http_get(url))
    items = (d.get("msgBody") or {}).get("itemList") or []
    h = d.get("msgHeader") or {}
    return items, f"{h.get('headerCd')} {h.get('headerMsg')}"


def seoul_route_id(target, key):
    """'7016' 같은 서울 버스 노선 번호를 busRouteId 로 바꿈. data/_routes.json 에 캐시."""
    if target.isdigit() and len(target) >= 9:  # 이미 busRouteId
        return target
    cache_f = DATA / "_routes.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    ck = "seoul:" + target
    if ck not in cache:
        items, st = _seoul_bus_get(
            "http://ws.bus.go.kr/api/rest/busRouteInfo/getBusRouteList"
            f"?serviceKey={urllib.parse.quote(key, safe='')}&strSrch={urllib.parse.quote(target)}&resultType=json")
        found = [it for it in items if str(it.get("busRouteNm", "")).strip() == target]
        if not found:
            raise RuntimeError(f"서울 버스 {target} 의 busRouteId 를 찾지 못함 ({st})")
        cache[ck] = str(found[0]["busRouteId"])
        cache_f.parent.mkdir(parents=True, exist_ok=True)
        cache_f.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"서울 버스 {target} -> busRouteId {cache[ck]}")
    return cache[ck]


def incheon_route_ids(target, key):
    """'536' 같은 노선 번호를 인천 routeId 로 바꿈. 결과는 data/_routes.json 에 캐시."""
    if target.isdigit() and len(target) >= 9:  # 이미 routeId
        return [target]
    cache_f = DATA / "_routes.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    if target not in cache:
        items, st = _incheon_get(
            "http://apis.data.go.kr/6280000/busRouteService/getBusRouteNo"
            f"?serviceKey={urllib.parse.quote(key, safe='')}&routeNo={urllib.parse.quote(target)}&numOfRows=20&pageNo=1")
        found = [it for it in items if str(it.get("ROUTENO", "")).strip() == target]
        if not found:
            raise RuntimeError(f"인천 노선 {target} 의 routeId 를 찾지 못함 ({st})")
        cache[target] = [str(it["ROUTEID"]) for it in found]
        cache_f.parent.mkdir(parents=True, exist_ok=True)
        cache_f.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"인천 노선 {target} -> routeId {cache[target]}")
    return cache[target]


def _xml_items(text):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    items = [{c.tag: c.text for c in it} for it in root.iter("itemList")] or \
            [{c.tag: c.text for c in it} for it in root.iter("item")]
    code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode")
    msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg")
    return items, f"{code} {msg}"


def save(api, rows, meta, day):
    out = DATA / api / f"{day}.jsonl.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "at", encoding="utf-8") as f:
        if not rows:  # 데이터 없음도 기록 (운행 없음 vs 수집 실패 구분용)
            f.write(json.dumps({**meta, "_empty": True}, ensure_ascii=False) + "\n")
        for r in rows:
            f.write(json.dumps({**meta, **r}, ensure_ascii=False) + "\n")


def run(calls, cfg, env, now):
    day = now.date().isoformat()
    caps = cfg["daily_cap"]
    for api, target, job in calls:
        group, env_name = API_KEYS[api]
        if not env.get(env_name, "").isascii() or not env.get(env_name):
            log(f"SKIP .env 의 {env_name} 가 비어 있거나 예시 문구입니다: {api} {target}")
            continue
        if calls_today(day).get(group, 0) >= caps.get(group, 0):
            log(f"SKIP 일일한도 {group} {caps.get(group)} 도달: {api} {target}")
            continue
        add_call(day, group)  # 실패한 호출도 한도에서 차감되므로 먼저 셈
        fetched = dt.datetime.now(KST).isoformat(timespec="seconds")
        try:
            rows, status = fetch(api, target, env)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as e:
            log(f"FAIL {api} {target}: {e}")
            continue
        save(api, rows, {"_fetched_at": fetched, "_job": job, "_target": target}, day)
        log(f"OK   {api} {target}: {len(rows)}건 ({status})")
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tick", action="store_true")
    g.add_argument("--plan", action="store_true")
    g.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = load_config()
    if a.plan:
        plan(cfg)
        return
    env = load_env()
    now = dt.datetime.now(KST).replace(second=0, microsecond=0)

    if a.test:
        seen, calls = set(), []
        for job in cfg["jobs"]:
            if job["targets"] and job["api"] not in seen:
                seen.add(job["api"])
                calls.append((job["api"], job["targets"][0], "test"))
        run(calls, cfg, env, now)
        return

    start, end = dt.date.fromisoformat(cfg["start_date"]), dt.date.fromisoformat(cfg["end_date"])
    if start <= now.date() <= end:
        run(due_calls(cfg, now), cfg, env, now)
        if now.minute == 5:  # 매시 5분에 시범 사이트 데이터 갱신
            try:
                import build_site
                build_site.main()
            except Exception as e:  # 사이트 갱신 실패가 수집을 막지 않게
                log(f"FAIL build_site: {e!r}")


if __name__ == "__main__":
    main()
