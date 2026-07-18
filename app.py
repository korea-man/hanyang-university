"""
route_map.py - 보행자 경로 안내: 계단/좁은길/공사구역 회피
공사 데이터: 행정안전부 생활안전지도 건설공사현황 (objtId=121, IF_0043)
실행: streamlit run route_map.py
"""
import json
import math
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import pandas as pd
import requests
import streamlit as st

ROUTE_JSON = Path(__file__).parent / "route.json"
TMAP_KEY = "YEWVxfrK4j8xTNQZURJ4z1Te4JTZs26v45fgmfn7"
SAFEMAP_KEY = "2VGKJCV5-2VGK-2VGK-2VGK-2VGKJCV5RE"
SAFEMAP_URL = "https://safemap.go.kr/openapi2/IF_0043"

STAIR_KW = ["계단", "육교", "지하보도", "에스컬레이터"]
NARROW_KW = ["보행자도로", "이면도로", "골목"]
NARROW_RT = {0, 22}
_M = 111320.0

st.set_page_config(page_title="보행자 경로", page_icon="🚶", layout="wide")

DEFAULT_START = (126.92365493654832, 37.556770374096615)
DEFAULT_END = (126.92432158129688, 37.55279861528311)
DEFAULT_WAYPTS = [(126.92774822, 37.55395475), (126.9257762, 37.55337145)]


# ─────────────────────────────────────────────
# 외부 API
# ─────────────────────────────────────────────
def call_tmap(start, end, search_option="0", pass_list=None):
    url = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1&callback=function"
    payload = {"startX": start[0], "startY": start[1], "angle": 20, "speed": 30,
               "endPoiId": "10001", "endX": end[0], "endY": end[1],
               "reqCoordType": "WGS84GEO", "startName": "%EC%B6%9C%EB%B0%9C",
               "endName": "%EB%8F%84%EC%B0%A9", "searchOption": str(search_option),
               "resCoordType": "WGS84GEO", "sort": "index"}
    if pass_list:
        payload["passList"] = "_".join(f"{lo:.8f},{la:.8f}" for lo, la in pass_list[:5])
    headers = {"accept": "application/json",
               "content-type": "application/json", "appKey": TMAP_KEY}
    r = requests.post(url, json=payload, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def merc2wgs(x, y):
    """EPSG:3857(Web Mercator) -> WGS84 위경도"""
    R = 6378137.0
    lon = math.degrees(x / R)
    lat = math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2)
    return lon, lat


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_construction(max_pages=60, rows=1000):
    """생활안전지도 건설공사현황 전체 페이징 수집 (하루 캐시)"""
    items = []
    for p in range(1, max_pages + 1):
        r = requests.get(SAFEMAP_URL,
                         params={"serviceKey": SAFEMAP_KEY, "pageNo": p,
                                 "numOfRows": rows, "returnType": "json"},
                         timeout=20)
        r.raise_for_status()
        body = r.json().get("body", {})
        batch = body.get("items", {}).get("item", []) or []
        if isinstance(batch, dict):        # 1건일 때 dict로 오는 경우 방어
            batch = [batch]
        items += batch
        if not batch or p * rows >= int(body.get("totalCount", 0)):
            break
    return items


def zones_near_route(items, coords, radius_m, buffer_m=500):
    """경로 주변(buffer) + 공사기간 진행중인 공사만 회피구역으로"""
    if not coords:
        return []
    today = date.today().strftime("%Y%m%d")
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    pad = buffer_m / _M
    lo0, lo1 = min(lons) - pad, max(lons) + pad
    la0, la1 = min(lats) - pad, max(lats) + pad
    zones = []
    for it in items:
        try:
            lon, lat = merc2wgs(float(it["x"]), float(it["y"]))
        except (TypeError, ValueError, KeyError):
            continue
        if not (lo0 <= lon <= lo1 and la0 <= lat <= la1):
            continue
        s = it.get("strwrk_de") or "00000000"
        e = it.get("compet_de") or "99999999"
        if not (s <= today <= e):
            continue
        zones.append({"lat": lat, "lon": lon, "radius": radius_m,
                      "name": it.get("cntwrk_nm") or "공사",
                      "addr": it.get("wrk_adres") or "", "end": e})
    return zones


# ─────────────────────────────────────────────
# 경로 파싱/기하
# ─────────────────────────────────────────────
def clean(t):
    try:
        return unquote(t) if t else ""
    except Exception:
        return t or ""


def parse_route(gj):
    pts, coords, lines, total = [], [], [], {"distance": 0, "time": 0}
    for f in gj.get("features", []):
        g, p = f["geometry"], f["properties"]
        if g["type"] == "Point":
            lon, lat = g["coordinates"]
            pts.append({"lat": lat, "lon": lon, "idx": p.get("pointIndex", 0),
                        "type": p.get("pointType", ""),
                        "desc": clean(p.get("description"))})
            if p.get("pointType") == "SP":
                total = {"distance": p.get("totalDistance", 0),
                         "time": p.get("totalTime", 0)}
        else:
            for c in g["coordinates"]:
                if not coords or coords[-1] != c:
                    coords.append(c)
            lines.append({"name": p.get("name", ""),
                          "distance": p.get("distance", 0),
                          "roadType": p.get("roadType", -1),
                          "desc": clean(p.get("description"))})
    return pts, coords, lines, total


def hav_m(a, b):
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    h = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(b[0] - a[0]) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def dotted_points(coords, spacing_m=12.0):
    if len(coords) < 2:
        return pd.DataFrame(columns=["lat", "lon"])
    pts, carry = [(coords[0][1], coords[0][0])], 0.0
    for i in range(len(coords) - 1):
        (lo1, la1), (lo2, la2) = coords[i], coords[i + 1]
        mlat = math.radians((la1 + la2) / 2)
        seg = math.hypot((lo2 - lo1) * _M * math.cos(mlat), (la2 - la1) * _M)
        if seg < 1e-9:
            continue
        d = spacing_m - carry
        while d <= seg:
            t = d / seg
            pts.append((la1 + (la2 - la1) * t, lo1 + (lo2 - lo1) * t))
            d += spacing_m
        carry = seg - (d - spacing_m)
    pts.append((coords[-1][1], coords[-1][0]))
    return pd.DataFrame(pts, columns=["lat", "lon"])


# ─────────────────────────────────────────────
# 회피 판정
# ─────────────────────────────────────────────
def violations(pts, lines, zones, a_st, a_nr, a_zn, coords):
    v = {"계단": [], "좁은길": [], "공사": []}
    if a_st:
        for p in pts:
            if any(k in p["desc"] for k in STAIR_KW):
                v["계단"].append(p["desc"])
    if a_nr:
        for l in lines:
            if l["roadType"] in NARROW_RT or any(k in l["name"] for k in NARROW_KW):
                v["좁은길"].append(f"{l['name'] or '미분류'} {l['distance']}m")
    hits = []
    if a_zn and zones:
        chk = ([tuple(c) for c in coords] if len(coords) < 2 else
               [(lo, la) for la, lo in
                dotted_points(coords, 10.0).itertuples(index=False)])
        for c in chk:
            for z in zones:
                if hav_m(c, (z["lon"], z["lat"])) <= z["radius"]:
                    hits.append(c)
                    break
        if hits:
            v["공사"].append(f"공사 구역 통과 지점 {len(hits)}곳")
    return v, hits


def score_of(v, total, hits):
    nar = sum(int(s.split()[-1][:-1]) if s.split()[-1].endswith("m") else 0
              for s in v["좁은길"])
    return len(v["계단"]) * 1000 + len(hits) * 5000 + nar * 2 + total["distance"]


def detour_point(hits, zones):
    if not hits or not zones:
        return None
    lon, lat = hits[len(hits) // 2]
    z = min(zones, key=lambda z: hav_m((lon, lat), (z["lon"], z["lat"])))
    dx, dy = lon - z["lon"], lat - z["lat"]
    n = math.hypot(dx, dy) or 1e-9
    push = (z["radius"] * 1.8) / _M
    return (z["lon"] + dx / n * push, z["lat"] + dy / n * push)


# ─────────────────────────────────────────────
# 기본 경로 준비 (없으면 TMAP으로 자동 생성)
# ─────────────────────────────────────────────
if not ROUTE_JSON.exists():
    try:
        gj0 = call_tmap(DEFAULT_START, DEFAULT_END, "0", DEFAULT_WAYPTS)
        ROUTE_JSON.write_text(json.dumps(gj0, ensure_ascii=False),
                              encoding="utf-8")
    except Exception as e:
        st.error(f"route.json 생성 실패: {e}")
        st.stop()

base_gj = json.loads(ROUTE_JSON.read_text(encoding="utf-8"))
b_pts, b_coords, b_lines, b_total = parse_route(base_gj)
start = next(((p["lon"], p["lat"]) for p in b_pts if p["type"] == "SP"),
             DEFAULT_START)
end = next(((p["lon"], p["lat"]) for p in b_pts if p["type"] == "EP"),
           DEFAULT_END)
waypts = [(p["lon"], p["lat"]) for p in sorted(b_pts, key=lambda x: x["idx"])
          if p["type"].startswith("PP")] or DEFAULT_WAYPTS


# ─────────────────────────────────────────────
# 사이드바
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 회피 옵션")
    a_st = st.checkbox("🪜 계단 피하기", value=False)
    a_nr = st.checkbox("↔️ 좁은 길 피하기 (대로 우선)", value=False)
    a_zn = st.checkbox("🚧 공사 구역 피하기 (생활안전지도)", value=False)
    radius = st.slider("공사장 회피 반경(m)", 30, 300, 100)
    st.header("🎨 표시")
    spacing = st.slider("점선 간격(m)", 5, 30, 12)
    apply_btn = st.button("옵션 적용 (경로 재탐색)", type="primary",
                          disabled=not (a_st or a_nr or a_zn),
                          use_container_width=True)

zones = []
if a_zn:
    with st.spinner("생활안전지도 건설공사 데이터 로딩 중..."):
        try:
            items = fetch_construction()
            zones = zones_near_route(items, b_coords, radius)
            st.sidebar.success(
                f"경로 주변 진행중 공사 {len(zones)}건 (전국 {len(items):,}건 중)")
        except Exception as e:
            st.sidebar.error(f"공사 데이터 로딩 실패: {e}")

ss = st.session_state
ss.setdefault("route", None)

# ─────────────────────────────────────────────
# 재탐색
# ─────────────────────────────────────────────
if apply_btn:
    opts = ([30, 4, 0] if a_st else [4, 0, 30] if a_nr else [0, 4, 30])
    best = None
    with st.spinner("회피 조건으로 경로 재탐색 중..."):
        for so in opts:
            try:
                gj = call_tmap(start, end, so, pass_list=waypts)
            except Exception as e:
                st.warning(f"searchOption={so} 실패: {e}")
                continue
            pts, coords, lines, total = parse_route(gj)
            v, hits = violations(pts, lines, zones, a_st, a_nr, a_zn, coords)
            sc = score_of(v, total, hits)
            cand = {"opt": so, "pts": pts, "coords": coords, "lines": lines,
                    "total": total, "viol": v, "hits": hits, "score": sc}
            if best is None or sc < best["score"]:
                best = cand
        if best and best["hits"] and len(waypts) < 5:
            dp = detour_point(best["hits"], zones)
            if dp:
                try:
                    gj = call_tmap(start, end, best["opt"],
                                   pass_list=waypts + [dp])
                    pts, coords, lines, total = parse_route(gj)
                    v, hits = violations(pts, lines, zones,
                                         a_st, a_nr, a_zn, coords)
                    sc = score_of(v, total, hits)
                    if sc < best["score"]:
                        best = {"opt": best["opt"], "pts": pts,
                                "coords": coords, "lines": lines,
                                "total": total, "viol": v,
                                "hits": hits, "score": sc}
                except Exception as e:
                    st.warning(f"우회 재탐색 실패: {e}")
    if best:
        ss["route"] = best
        st.rerun()

if ss["route"] and st.sidebar.button("기본 경로로 되돌리기",
                                     use_container_width=True):
    ss["route"] = None
    st.rerun()

# ─────────────────────────────────────────────
# 지도 + 결과
# ─────────────────────────────────────────────
st.title("🚶 보행자 경로 안내")

if ss["route"]:
    r = ss["route"]
    pts, coords, lines, total = r["pts"], r["coords"], r["lines"], r["total"]
    src = f"재탐색 (searchOption={r['opt']})"
else:
    pts, coords, lines, total = b_pts, b_coords, b_lines, b_total
    src = "기본 경로 (route.json)"

c1, c2, c3 = st.columns(3)
c1.metric("총 거리", f"{total['distance']:,} m")
c2.metric("예상 소요", f"{total['time'] // 60}분 {total['time'] % 60}초")
c3.metric("경로 출처", src)

dots = dotted_points(coords, spacing).assign(color="#2980b9", size=3)
mk = pd.DataFrame([
    {"lat": p["lat"], "lon": p["lon"],
     "color": {"SP": "#2ecc71", "EP": "#e74c3c"}.get(p["type"], "#f39c12"),
     "size": 14 if p["type"] in ("SP", "EP") else 10}
    for p in pts if p["type"] in ("SP", "EP") or p["type"].startswith("PP")])
layers = [dots, mk]

if zones:
    ring = []
    for z in zones:
        for deg in range(0, 360, 12):
            rad = math.radians(deg)
            ring.append({
                "lat": z["lat"] + (z["radius"] / _M) * math.sin(rad),
                "lon": z["lon"] + (z["radius"] / (_M * math.cos(
                    math.radians(z["lat"])))) * math.cos(rad),
                "color": "#e67e22", "size": 2.5})
    layers.append(pd.DataFrame(ring))

st.map(pd.concat(layers, ignore_index=True), latitude="lat", longitude="lon",
       color="color", size="size", zoom=15)

if zones:
    with st.expander(f"🚧 경로 주변 진행중 공사 {len(zones)}건 (생활안전지도)"):
        for z in zones:
            st.write(f"・**{z['name']}** — {z['addr']} (준공 예정 {z['end']})")

if ss["route"]:
    v = ss["route"]["viol"]
    if any(v.values()):
        st.warning("⚠️ 일부 회피 조건을 만족하지 못했습니다.")
        for k, its in v.items():
            if its:
                with st.expander(f"{k} 관련 {len(its)}건"):
                    for it in its:
                        st.write("・", it)
    else:
        st.success("✅ 설정한 회피 조건을 모두 만족하는 경로입니다.")
elif a_st or a_nr or a_zn:
    v, hits = violations(b_pts, b_lines, zones, a_st, a_nr, a_zn, b_coords)
    if any(v.values()):
        st.info("기본 경로가 선택한 회피 조건에 걸립니다. "
                "'옵션 적용'으로 재탐색하세요.")

with st.expander("📋 턴바이턴 안내"):
    for p in sorted(pts, key=lambda x: x["idx"]):
        ic = {"SP": "🟢", "EP": "🔴"}.get(
            p["type"], "🟠" if p["type"].startswith("PP") else "🔵")
        st.markdown(f"{ic} `{p['idx']:>2}` — {p['desc']}")
