"""피로도 계산에 쓸 '변하지 않는' 자료를 내려받아 data/static/ 에 CSV(UTF-8)로 정리합니다.

    py prepare_static.py

1. 환승 거리·소요시간   공공데이터포털 15044419 (서울교통공사, 1~8호선 환승역 145곳, 보행속도 1.2m/s 기준)
2. 칸 혼잡도 (30분 단위) 서울 열린데이터광장 OA-12928 (서울교통공사 1~8호선, 정원 대비 승차인원 %, 평일/토/일)
3. 시간대별 승하차 인원  서울 열린데이터광장 CardSubwayTime API (1~9호선, 월별, SEOUL_OPENAPI_KEY 필요)
4. 역별 시간표          서울 열린데이터광장 SearchSTNTimeTableByIDService (사례 역들, 평일/토/휴일, 양방향)
"""
import csv
import datetime as dt
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from collector import http_get, load_env

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "static"
UA = {"User-Agent": "Mozilla/5.0"}


def download(url, data=None, referer=None):
    headers = dict(UA)
    if referer:
        headers["Referer"] = referer
    body = urllib.parse.urlencode(data).encode() if data else None
    with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=60) as r:
        return r.read()


def write_csv(name, header, rows):
    with open(OUT / name, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"{name}: {len(rows)}행")


# ---------- 1. 환승 거리 ----------

def transfer_distance():
    pk, detail = "15044419", "uddi:7008c675-928f-41d6-9a01-b3541f78466b"
    meta = json.loads(download("https://www.data.go.kr/tcs/dss/selectFileDataDownload.do",
                               {"publicDataPk": pk, "publicDataDetailPk": detail, "fileDetailSn": "1"}))
    fid = meta["atchFileId"]
    raw = download(f"https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId={fid}&fileDetailSn=1")
    text = raw.decode("cp949") if not raw.startswith(b"\xef\xbb\xbf") else raw.decode("utf-8-sig")
    rows = list(csv.reader(text.splitlines()))
    out = []
    for r in rows[1:]:  # 연번,호선,환승역명,환승노선,환승거리(m),환승소요시간(mm:ss)
        if len(r) < 6:
            continue
        m, s = (r[5].split(":") + ["0"])[:2]
        out.append([r[1] + "호선", r[2], r[3], int(r[4]), int(m) * 60 + int(s)])
    write_csv("transfer_distance.csv", ["호선", "환승역", "환승노선", "거리_m", "소요_초"], out)


# ---------- 2. 혼잡도 ----------

def _xlsx_sheets(raw_path):
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    z = zipfile.ZipFile(raw_path)
    ss = [''.join(t.itertext()) for t in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("a:si", ns)]
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    for sh in ET.fromstring(z.read("xl/workbook.xml")).find("a:sheets", ns):
        target = rels[sh.get("{%s}id" % ns["r"])].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for r in ET.fromstring(z.read(target)).findall(".//a:sheetData/a:row", ns):
            vals = []
            for c in r.findall("a:c", ns):
                v = c.find("a:v", ns)
                v = v.text if v is not None else ""
                vals.append(ss[int(v)] if c.get("t") == "s" else v)
            rows.append(vals)
        yield sh.get("name"), rows


def crowding():
    page = "https://data.seoul.go.kr/dataList/OA-12928/S/1/datasetView.do"
    html = download(page).decode("utf-8", "replace")
    # 가장 최근 파일의 seq 찾기 (파일명 끝 날짜 기준)
    files = re.findall(r"지하철혼잡도정보_(\d{8})\.(?:xlsx|csv).{0,400}?downloadFile\('(\d+)'\)", html, re.S)
    date, seq = max(files)
    raw = OUT / f"crowding_{date}.xlsx"
    raw.write_bytes(download("https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?&useCache=false",
                             {"infId": "OA-12928", "seqNo": "", "seq": seq, "infSeq": "1"}, referer=page))
    out = []
    for name, rows in _xlsx_sheets(raw):
        head = rows[0]
        for r in rows[1:]:
            if len(r) < 5:
                continue
            for slot, v in zip(head[4:], r[4:]):
                if v != "":
                    out.append([name, r[0], r[2], r[3], slot[:5], round(float(v), 1)])
    write_csv("crowding.csv", ["요일", "호선", "역", "상하구분", "시작", "혼잡도_pct"], out)
    print(f"  (기준일 {date})")


# ---------- 3. 승하차 인원 ----------

def ridership():
    key = load_env().get("SEOUL_OPENAPI_KEY", "")
    month = dt.date.today().replace(day=1)
    for _ in range(4):  # 최근 달부터 거꾸로, 데이터 있는 달 찾기
        month = (month - dt.timedelta(days=1)).replace(day=1)
        ym = month.strftime("%Y%m")
        rows, start = [], 1
        while True:
            d = json.loads(http_get(f"http://openapi.seoul.go.kr:8088/{key}/json/CardSubwayTime/{start}/{start + 999}/{ym}"))
            body = d.get("CardSubwayTime") or {}
            rows += body.get("row") or []
            if start + 999 >= int(body.get("list_total_count") or 0):
                break
            start += 1000
        if rows:
            break
    out = []
    for r in rows:
        for h in range(0, 24):
            on, off = r.get(f"HR_{h}_GET_ON_NOPE"), r.get(f"HR_{h}_GET_OFF_NOPE")
            if on is not None:
                out.append([ym, r["SBWY_ROUT_LN_NM"], r["STTN"], h, int(on), int(off or 0)])
    write_csv("ridership_hourly.csv", ["월", "호선", "역", "시", "승차", "하차"], out)


# ---------- 4. 시간표 (= 지도 앱이 쓰는 기준 시간) ----------

# 사례 구간의 양 끝역 + 도착 안내 수집역. 역 하나에 노선이 여럿이면 1~9호선 전부 받음
TT_STATIONS = ["동암", "신도림", "구일", "시청", "신촌", "사당", "동대문역사문화공원", "고속터미널", "종로3가",
               "종합운동장", "왕십리", "올림픽공원", "강남", "김포공항", "여의도", "가산디지털단지", "홍대입구",
               "성수", "건대입구", "공덕"]


def timetable():
    key = load_env().get("SEOUL_OPENAPI_KEY", "")
    q = urllib.parse.quote

    def api(svc, args):
        d = json.loads(http_get(f"http://openapi.seoul.go.kr:8088/{key}/json/{svc}/{args}"))
        return (d.get(svc) or {}).get("row") or []

    def sec(s):
        h, m, x = (int(v) for v in s.split(":"))
        t = h * 3600 + m * 60 + x
        return t + 86400 if t < 3 * 3600 else t  # 새벽 0~3시는 전날 운행의 연장

    out = []
    for name in TT_STATIONS:
        for st in api("SearchInfoBySubwayNameService", f"1/20/{q(name)}"):
            m = re.match(r"0(\d)호선", st.get("LINE_NUM", ""))
            if not m:
                continue
            line = "100" + m.group(1)
            for week in ("1", "2", "3"):        # 평일, 토요일, 휴일
                for way in ("1", "2"):          # 상행·내선, 하행·외선
                    for r in api("SearchSTNTimeTableByIDService", f"1/1000/{st['STATION_CD']}/{week}/{way}"):
                        no = re.sub(r"\D", "", r.get("TRAIN_NO") or "")
                        arr, dep = r.get("ARRIVETIME") or "00:00:00", r.get("LEFTTIME") or "00:00:00"
                        if not no or (arr == "00:00:00" and dep == "00:00:00"):
                            continue
                        t = sec(dep if arr == "00:00:00" else arr)  # 출발역은 도착 시각이 없음
                        out.append([line, name, week, way, int(no), t, r.get("EXPRESS_YN") == "D", r.get("SUBWAYENAME")])
    write_csv("timetable.csv", ["노선", "역", "요일", "방향", "열차번호", "도착_초", "급행", "행선지"], out)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in (transfer_distance, crowding, ridership, timetable):
        try:
            f()
        except Exception as e:  # 하나 실패해도 나머지는 받음
            print(f"{f.__name__} 실패: {e!r}"[:300])


if __name__ == "__main__":
    main()
