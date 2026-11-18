"""수집한 원본(data/)을 정리해서 시범 사이트용 web/data.js 를 만듭니다.

    py build_site.py        → web/data.js 생성 후 web/index.html 을 브라우저로 열면 됨

정리 단계
1. 열차 위치 스냅샷 → 열차별 궤적(역 ID, 시각)   : 어느 역→어느 역이든 실제 소요 시간 계산용
2. 도착 표시(N초 후 / N번째 전역) ↔ 실제 도착 매칭 : 표시가 얼마나 틀리는지
3. 수집 로그 → 날짜×시간 수집 현황               : 빠진 시간 확인
"""
import datetime as dt
import gzip
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SITE = ROOT / "web"
SPLIT_GAP = 40 * 60      # 같은 열차번호라도 40분 이상 끊기면 다른 운행으로 봄
MAX_BRACKET = 12 * 60    # 관측 두 점 사이가 12분 이내일 때만 보간 (수집 간격 8~10분)


def read_rows(api):
    for f in sorted((DATA / api).glob("*.jsonl.gz")):
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        pass
        except (EOFError, OSError):  # 수집기가 쓰는 중인 파일의 끝부분
            pass


def ts(s):
    """'2026-11-16 23:05:05' 또는 ISO → KST 기준 epoch 초 (naive 그대로 비교만 하면 됨)"""
    s = s[:19].replace("T", " ")
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()


def service_day(t):
    """새벽 3시 이전은 전날 운행으로 봄. (날짜, 그 날짜 0시 기준 초)"""
    d = dt.datetime.fromtimestamp(t - 3 * 3600, dt.timezone.utc).date()
    base = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()
    return d.isoformat(), int(t - base)


def branch(sid):
    return sid // 1000  # 1001000154 → 1001000 (같은 가지 안에서만 역 순서 비교)


# 2호선 본선은 순환선: 충정로(243) 다음이 시청(201). 번호가 끊기는 곳을 이어 붙여서 비교함
LOOP = {1002000: 43}


def step(a, b):
    """a→b 역 번호 차이. 순환선이면 짧은 쪽으로 감아서 계산"""
    d, n = b - a, LOOP.get(branch(a))
    if n and branch(a) == branch(b):
        d = (d + n // 2) % n - n // 2
    return d


def unwrap(pts):
    """[(sid, t)…] → [(이어 붙인 번호, t)…]. 순환선이 아니면 그대로"""
    out = []
    for s, t in pts:
        out.append((out[-1][0] + step(prev, s) if out else s, t))
        prev = s
    return out


# ---------- 1. 궤적 ----------

def build_trajectories():
    events = defaultdict(dict)        # (line, trainNo) → {(statnId, t): (express, terminal)}
    stations = defaultdict(dict)      # line → {statnId: 이름}
    polls = defaultdict(set)          # line 이름 → 수집 시각들
    for r in read_rows("subway_position"):
        if r.get("_empty") or "trainNo" not in r:
            if r.get("_target"):
                polls[r["_target"]].add(r["_fetched_at"])
            continue
        polls[r["_target"]].add(r["_fetched_at"])
        if r.get("trainSttus") == "3" or not r.get("recptnDt"):  # 3=전역출발: 역 사이라 제외
            continue
        sid = int(r["statnId"])
        stations[r["subwayId"]][sid] = r["statnNm"].removesuffix("지선") or r["statnNm"]  # '성수지선' → '성수'
        events[(r["subwayId"], r["trainNo"])][(sid, ts(r["recptnDt"]))] = (r.get("directAt") == "1", r.get("statnTnm"))

    trains = []
    for (line, no), ev in events.items():
        pts = sorted(ev.items(), key=lambda kv: kv[0][1])
        runs, cur = [], []
        for (sid, t), meta in pts:
            if cur and (t - cur[-1][1] > SPLIT_GAP or _reverses(cur, sid)):
                runs.append(cur)
                cur = []
            cur.append((sid, t, meta))
        if cur:
            runs.append(cur)
        for run in runs:
            first = {}
            for sid, t, _ in run:   # 역마다 가장 이른 시각 (진입/도착)
                first.setdefault(sid, t)
            if len(first) < 2:
                continue
            day, _ = service_day(run[0][1])
            base = ts(day + " 00:00:00")
            express = any(m[0] for _, _, m in run)
            terminal = max((m[1] for _, _, m in run if m[1]), key=lambda x: sum(1 for *_, m in run if m[1] == x), default="")
            trains.append({"l": line, "n": no, "d": day, "x": int(express), "e": terminal,
                           "p": sorted([[sid, int(t - base)] for sid, t in first.items()], key=lambda p: p[1])})
    return trains, stations, polls


def _reverses(cur, sid):
    same = [s for s, _, _ in cur if branch(s) == branch(sid)]
    if len(same) < 2:
        return False
    a, b = same[-2], same[-1]
    return step(a, b) * step(b, sid) < 0


def time_at(train, sid):
    """궤적에서 역 sid 도착 시각(초) 보간. 없으면 None"""
    pts = [p for p in train["p"] if branch(p[0]) == branch(sid)]
    for s, t in pts:
        if s == sid:
            return t
    n = LOOP.get(branch(sid))
    pts = unwrap(pts)
    for v in ([sid + k * n for k in range(-2, 3)] if n else [sid]):
        for (s1, t1), (s2, t2) in zip(pts, pts[1:]):
            if (s1 - v) * (s2 - v) < 0 and t2 - t1 <= MAX_BRACKET:
                return t1 + (t2 - t1) * (v - s1) / (s2 - s1)
    return None


# ---------- 2. 도착 표시 vs 실제 ----------

def build_predictions(trains):
    index = defaultdict(list)
    for tr in trains:
        index[(tr["l"], tr["n"])].append(tr)

    seen, eta, nth, age = set(), [], [], []
    for r in read_rows("subway_arrival"):
        if r.get("_empty") or not r.get("btrainNo") or not r.get("recptnDt"):
            continue
        key = (r["statnId"], r["btrainNo"], r["recptnDt"], r.get("arvlMsg2"))
        rt, ft = ts(r["recptnDt"]), ts(r["_fetched_at"])
        age.append({"s": r["statnNm"], "a": round((ft - rt) / 60, 1)})
        if key in seen:
            continue
        seen.add(key)
        sid = int(r["statnId"])
        actual = None
        for tr in index.get((r["subwayId"], r["btrainNo"]), []):
            t = time_at(tr, sid)
            if t is not None:
                at = ts(tr["d"] + " 00:00:00") + t
                if -300 < at - rt < 3600:  # 같은 운행인지 확인
                    actual = at
                    break
        if actual is None:
            continue
        express = r.get("btrainSttus") == "급행"
        sec = int(r.get("barvlDt") or 0)
        m = re.match(r"\[(\d+)\]번째 전역", r.get("arvlMsg2") or "")
        day, tod = service_day(rt)
        common = {"s": r["statnNm"], "l": r["subwayId"], "n": r["btrainNo"], "x": int(express), "d": day, "h": tod}
        if sec > 0:
            eta.append({**common, "shown": round(sec / 60, 1), "err": round((actual - (rt + sec)) / 60, 1)})
        elif m:
            nth.append({**common, "k": int(m.group(1)), "wait": round((actual - rt) / 60, 1)})
    return eta, nth, age


# ---------- 3. 시간표(= 앱 기준 시간) vs 실제 ----------

def load_timetable():
    """(노선, 역 이름, 요일구분, 열차번호) → 시간표 도착 초. prepare_static.py 가 만든 CSV"""
    f = DATA / "static" / "timetable.csv"
    tt = {}
    if f.exists():
        import csv
        with open(f, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                tt[(r["노선"], r["역"], r["요일"], int(r["열차번호"]))] = int(r["도착_초"])
    return tt


def week_tag(day):
    wd = dt.date.fromisoformat(day).weekday()
    return "1" if wd < 5 else ("2" if wd == 5 else "3")


def build_schedule(trains, stations):
    """열차마다 시간표가 있는 역의 '시간표 시각'을 붙이고(tr['s']), 역별 지연 목록을 만듦.
    실제 시각은 궤적에서 그 역에 처음 잡힌 시각(진입·도착)이라 시간표 도착보다 수십 초 이를 수 있음"""
    tt = load_timetable()
    names = {(l, n) for l, n, _, _ in tt}
    ids = defaultdict(list)
    for l, st in stations.items():
        for sid, n in st.items():
            if (l, n) in names:
                ids[(l, n)].append(sid)
    out = []
    for tr in trains:
        try:
            no = int(re.sub(r"\D", "", tr["n"]))
        except ValueError:
            continue
        wk, sched = week_tag(tr["d"]), {}
        for (l, n), sids in ids.items():
            if l != tr["l"]:
                continue
            plan = tt.get((l, n, wk, no))
            if plan is None:
                continue
            for sid in sids:
                t = time_at(tr, sid)
                if t is not None and abs(t - plan) <= 20 * 60:  # 20분 넘게 어긋나면 다른 운행으로 봄
                    sched[str(sid)] = plan
                    out.append({"s": n, "l": l, "n": tr["n"], "x": tr["x"], "d": tr["d"], "h": plan,
                                "dl": round((t - plan) / 60, 1)})
                    break
        if sched:
            tr["s"] = sched
    return out


def load_naver():
    """naver_check.csv 에 직접 옮겨 적은 네이버 지도 탑승 시간 (채운 줄만)"""
    f = ROOT / "naver_check.csv"
    out = []
    if f.exists():
        import csv
        with open(f, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                m = re.match(r"(\d)호선", r.get("노선", ""))
                try:
                    mins = float(r.get("네이버_탑승분") or "")
                except ValueError:
                    continue
                if m:
                    out.append({"l": "100" + m.group(1), "o": r["출발역"], "d": r["도착역"], "w": r["요일"],
                                "t": r["출발시각"], "m": mins})
    return out


# ---------- 4. 수집 현황 ----------

def build_health():
    f = DATA / "_log" / "collector.log"
    cells = defaultdict(lambda: {"ok": 0, "fail": 0, "skip": 0})
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"(\d{4}-\d\d-\d\d) (\d\d):\d\d:\d\d (OK|FAIL|SKIP)", line)
            if m:
                cells[(m.group(1), int(m.group(2)))][m.group(3).lower()] += 1
    return [{"d": d, "h": h, **v} for (d, h), v in sorted(cells.items())]


def main():
    trains, stations, polls = build_trajectories()
    eta, nth, age = build_predictions(trains)
    tt = build_schedule(trains, stations)
    lines = {"1001": "1호선", "1002": "2호선", "1003": "3호선", "1004": "4호선", "1005": "5호선",
             "1006": "6호선", "1007": "7호선", "1008": "8호선", "1009": "9호선", "1063": "경의중앙선"}
    by_name = {v: k for k, v in lines.items()}
    out = {
        "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "lines": lines,
        "stations": {l: {str(k): v for k, v in sorted(s.items())} for l, s in stations.items()},
        # 노선별 수집 시각 [운행일, 초] — 배차 간격이 '수집 공백' 때문에 커 보이는지 거르는 데 씀
        "polls": {by_name.get(l, l): sorted(list(service_day(ts(p))) for p in v) for l, v in polls.items()},
        "trains": trains, "tt": tt, "naver": load_naver(), "eta": eta, "nth": nth, "age": age, "health": build_health(),
    }
    SITE.mkdir(exist_ok=True)
    (SITE / "data.js").write_text("window.DATA = " + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n",
                                  encoding="utf-8")
    print(f"열차 궤적 {len(trains)}개, 도착 표시 매칭 {len(eta) + len(nth)}건, 시간표 비교 {len(tt)}건, web/data.js 생성")


if __name__ == "__main__":
    main()
