"""
route_map.py - 보행자 경로 안내 (v2)
- 출발지/도착지/경유지: 주소·지명·건물명 텍스트 입력 (TMAP 통합검색으로 좌표 변환)
- 출발지 현재 위치 옵션 (브라우저 위치)
- 계단/좁은길 회피 + 공사구역(생활안전지도 건설공사현황) 회피
실행: streamlit run route_map.py
필요: pip install streamlit pandas requests streamlit-geolocation
"""
import math
from datetime import date
from urllib.parse import unquote

import pandas as pd
import requests
import streamlit as st

TMAP_KEY = "YEWVxfrK4j8xTNQZURJ4z1Te4JTZs26v45fgmfn7"
SAFEMAP_KEY = "2VGKJCV5-2VGK-2VGK-2VGK-2VGKJCV5RE"
SAFEMAP_URL = "https://safemap.go.kr/openapi2/IF_0043"

STAIR_KW = ["계단", "육교", "지하보도", "에스컬레이터"]
NARROW_KW = ["보행자도로", "이면도로", "골목"]
NARROW_RT = {0, 22}
CROSS_FT = {"15"}                 # facilityType 15 = 횡단보도
STAIR_FT = {"12", "14", "17"}     # 12 육교, 14 지하보도, 17 계단
CROSS_TT = {211, 212, 213}        # turnType: 횡단보도/좌측/우측 횡단보도
_M = 111320.0

st.set_page_config(page_title="보행자 경로", page_icon="🚶", layout="wide")

try:
    from streamlit_geolocation import streamlit_geolocation
    HAS_GEO = True
except ImportError:
    HAS_GEO = False


# ─────────────────────────────────────────────
# 외부 API
# ─────────────────────────────────────────────
def geocode(keyword):
    """주소/지명/건물명 -> (lon, lat, 표시명). TMAP 통합검색(POI)."""
    r = requests.get(
        "https://apis.openapi.sk.com/tmap/pois",
        params={"version": 1, "searchKeyword": keyword, "count": 1,
                "resCoordType": "WGS84GEO", "reqCoordType": "WGS84GEO"},
        headers={"accept": "application/json", "appKey": TMAP_KEY},
        timeout=10)
    r.raise_for_status()
    pois = (r.json().get("searchPoiInfo", {})
            .get("pois", {}).get("poi", []))
    if not pois:
        return None
    p = pois[0]
    lat = float(p.get("frontLat") or p.get("noorLat"))
    lon = float(p.get("frontLon") or p.get("noorLon"))
    addr = " ".join(x for x in [p.get("upperAddrName"), p.get("middleAddrName"),
                                p.get("roadName")] if x)
    return lon, lat, f"{p.get('name', keyword)} ({addr})"


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
    R = 6378137.0
    return (math.degrees(x / R),
            math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2))


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_construction(max_pages=60, rows=1000):
    """생활안전지도 건설공사현황 전체 수집.
    최초 1회만 실제 다운로드(진행바 표시), 이후 24시간 서버 캐시로 즉시 반환."""
    items = []
    bar = st.progress(0.0, text="🚧 건설공사 데이터 로딩 중... (최초 1회)")
    try:
        for p in range(1, max_pages + 1):
            r = requests.get(SAFEMAP_URL,
                             params={"serviceKey": SAFEMAP_KEY, "pageNo": p,
                                     "numOfRows": rows, "returnType": "json"},
                             timeout=20)
            r.raise_for_status()
            body = r.json().get("body", {})
            total_cnt = int(body.get("totalCount", 0)) or 1
            batch = body.get("items", {}).get("item", []) or []
            if isinstance(batch, dict):
                batch = [batch]
            items += batch
            bar.progress(min(len(items) / total_cnt, 1.0),
                         text=f"🚧 건설공사 데이터 로딩 중... "
                              f"{len(items):,}/{total_cnt:,}건")
            if not batch or p * rows >= total_cnt:
                break
    finally:
        bar.empty()
    return items


def zones_near(items, anchors, radius_m, buffer_m=1000):
    """앵커(출발/경유/도착) 주변 + 오늘 진행중인 공사만 회피구역으로"""
    if not anchors:
        return []
    today = date.today().strftime("%Y%m%d")
    lons = [a[0] for a in anchors]
    lats = [a[1] for a in anchors]
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
# 경로 파싱/기하/회피 판정
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
                        "desc": clean(p.get("description")),
                        "turnType": p.get("turnType", 0),
                        "ft": str(p.get("facilityType", ""))})
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
                          "desc": clean(p.get("description")),
                          "ft": str(p.get("facilityType", "")),
                          "coords": g["coordinates"]})
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


def is_crosswalk(l):
    """횡단보도 구간 (좁은 길에서 제외하고 별도 표시)"""
    return l.get("ft") in CROSS_FT


def is_stair_line(l):
    """계단/육교/지하보도 구간 (좁은 길이 아니라 '계단' 위반으로 계상)"""
    return l.get("ft") in STAIR_FT


def is_narrow(l):
    if is_crosswalk(l) or is_stair_line(l):
        return False
    return l["roadType"] in NARROW_RT or any(k in l["name"] for k in NARROW_KW)


def violations(pts, lines, zones, a_st, a_nr, a_zn, coords):
    v = {"계단": [], "좁은길": [], "공사": []}
    if a_st:
        for p in pts:
            if any(k in p["desc"] for k in STAIR_KW):
                v["계단"].append(p["desc"])
        for l in lines:
            if is_stair_line(l):
                ftn = {"12": "육교", "14": "지하보도", "17": "계단"}.get(l["ft"], "계단시설")
                v["계단"].append(f"{ftn} 구간 {l['distance']}m")
    if a_nr:
        for l in lines:
            if is_narrow(l):
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


def detour_point(hits, zones, factor=1.8):
    """위반 지점을 구역 중심 반대 방향으로 radius*factor 미터 밀어낸 우회점.
    경도는 위도에 따라 미터 환산이 달라지므로 cos(lat) 보정."""
    if not hits or not zones:
        return None
    lon, lat = hits[len(hits) // 2]
    z = min(zones, key=lambda z: hav_m((lon, lat), (z["lon"], z["lat"])))
    coslat = math.cos(math.radians(z["lat"]))
    dx_m = (lon - z["lon"]) * _M * coslat          # 미터 단위 방향 벡터
    dy_m = (lat - z["lat"]) * _M
    n = math.hypot(dx_m, dy_m) or 1e-9
    dist = z["radius"] * factor                    # 목표 밀어내기 거리(m)
    return (z["lon"] + (dx_m / n * dist) / (_M * coslat),
            z["lat"] + (dy_m / n * dist) / _M)


def insert_detour(waypts, dp, start, end):
    """출발-경유-도착 앵커 중 dp와 가장 가까운 구간 사이에 삽입"""
    anchors = [start] + list(waypts) + [end]
    bi, bd = 0, float("inf")
    for i in range(len(anchors) - 1):
        mid = ((anchors[i][0] + anchors[i + 1][0]) / 2,
               (anchors[i][1] + anchors[i + 1][1]) / 2)
        d = hav_m(dp, mid)
        if d < bd:
            bi, bd = i, d
    out = list(waypts)
    out.insert(bi, dp)
    return out


def find_route(start, end, waypts, zones, a_st, a_nr, a_zn):
    """회피 조건 만족 경로 탐색 (다중 searchOption 비교 + 우회 재탐색)"""
    opts = ([30, 4, 0] if a_st else [4, 0, 30] if a_nr
            else [0] if not a_zn else [0, 4, 30])
    best, cands, errs, detour = None, [], [], None
    for so in opts:
        try:
            gj = call_tmap(start, end, so, pass_list=waypts)
        except Exception as e:
            errs.append(f"searchOption={so}: {e}")
            continue
        pts, coords, lines, total = parse_route(gj)
        v, hits = violations(pts, lines, zones, a_st, a_nr, a_zn, coords)
        sc = score_of(v, total, hits)
        cand = {"opt": so, "pts": pts, "coords": coords, "lines": lines,
                "total": total, "viol": v, "hits": hits, "score": sc}
        cands.append(cand)
        if best is None or sc < best["score"]:
            best = cand
    # 공사 구역을 지나면: 우회 경유지를 반복 삽입해 돌아서라도 회피
    #   반복마다 밀어내는 거리를 키움 (반경의 1.8배 → 2.6배 → 3.4배)
    detours = []
    base_opt = best["opt"] if best else None
    wp_cur = list(waypts)
    it = 0
    while (best and best["hits"] and len(wp_cur) < 5 and it < 3):
        it += 1
        dp = detour_point(best["hits"], zones, factor=1.0 + 0.8 * it)
        if not dp:
            break
        wp_cur = insert_detour(wp_cur, dp, start, end)
        try:
            gj = call_tmap(start, end, base_opt, pass_list=wp_cur)
        except Exception as e:
            errs.append(f"우회 {it}차 재탐색: {e}")
            break
        pts, coords, lines, total = parse_route(gj)
        v, hits = violations(pts, lines, zones, a_st, a_nr, a_zn, coords)
        sc = score_of(v, total, hits)
        cands.append({"opt": f"{base_opt}+우회{it}", "pts": pts,
                      "coords": coords, "lines": lines, "total": total,
                      "viol": v, "hits": hits, "score": sc})
        # 공사 통과가 줄었으면 채택 (거리가 늘어도 회피가 우선: 페널티 5000/지점)
        if len(hits) < len(best["hits"]) or sc < best["score"]:
            best = {"opt": f"{base_opt}+우회{it}", "pts": pts, "coords": coords,
                    "lines": lines, "total": total, "viol": v,
                    "hits": hits, "score": sc}
            detours.append(dp)
        else:
            wp_cur = [w for w in wp_cur if w != dp]   # 효과 없으면 되돌림
    return best, cands, errs, detours


# ─────────────────────────────────────────────
# 사이드바 - 지점 입력 + 회피 옵션
# ─────────────────────────────────────────────
ss = st.session_state
ss.setdefault("route", None)
ss.setdefault("labels", {})
ss.setdefault("zones", [])
ss.setdefault("cands", [])
ss.setdefault("detours", [])
ss.setdefault("opts_used", (False, False, False))


with st.sidebar:
    st.header("📍 지점 입력")
    st.caption("주소, 지명, 건물명 무엇이든 입력하세요 (예: 홍대입구역, 연세대학교)")

    use_cur = st.checkbox("출발지: 현재 위치 사용", value=False,
                          disabled=not HAS_GEO)
    if not HAS_GEO:
        st.caption("`pip install streamlit-geolocation` 후 현재 위치 사용 가능")
    cur_loc = None
    if use_cur and HAS_GEO:
        st.caption("아래 버튼을 눌러 위치 권한을 허용하세요")
        loc = streamlit_geolocation()
        if loc and loc.get("latitude"):
            cur_loc = (loc["longitude"], loc["latitude"])
            st.success(f"현재 위치 확인: ({loc['latitude']:.5f}, "
                       f"{loc['longitude']:.5f})")

    start_kw = st.text_input("출발지", value="", placeholder="예: 홍대입구역 2번출구",
                             disabled=bool(cur_loc))
    end_kw = st.text_input("도착지", value="", placeholder="예: 홍익대학교 정문")
    wp_text = st.text_area("경유지 (한 줄에 하나, 최대 5곳)", value="",
                           placeholder="예:\n상상마당\nKB국민은행 서교동지점",
                           height=90)

    st.header("⚙️ 회피 옵션")
    a_st = st.checkbox("🪜 계단 피하기", value=False)
    a_nr = st.checkbox("↔️ 좁은 길 피하기 (대로 우선)", value=False)
    a_zn = st.checkbox("🚧 공사 구역 피하기 (생활안전지도)", value=False)
    radius = st.slider("공사장 회피 반경(m)", 30, 300, 100)
    st.caption("공사 데이터는 탐색 시 자동 로드됩니다 "
               "(최초 1회만 다운로드, 24시간 캐시)")
    if st.button("공사 데이터 캐시 비우기"):
        fetch_construction.clear()
        st.toast("캐시를 비웠습니다. 다음 탐색 때 새로 받습니다.")

    st.header("🎨 표시")
    spacing = st.slider("점선 간격(m)", 5, 30, 12)

    go = st.button("🔍 경로 탐색", type="primary", use_container_width=True)

# ─────────────────────────────────────────────
# 탐색 실행
# ─────────────────────────────────────────────
if go:
    labels = {}
    errors = []

    # 1) 출발지: 현재 위치 또는 지오코딩
    if cur_loc:
        start = cur_loc
        labels["출발"] = "현재 위치"
    elif start_kw.strip():
        g = geocode(start_kw.strip())
        if g:
            start, labels["출발"] = (g[0], g[1]), g[2]
        else:
            errors.append(f"출발지 '{start_kw}' 검색 결과가 없습니다.")
            start = None
    else:
        errors.append("출발지를 입력하거나 현재 위치를 사용하세요.")
        start = None

    # 2) 도착지
    if end_kw.strip():
        g = geocode(end_kw.strip())
        if g:
            end, labels["도착"] = (g[0], g[1]), g[2]
        else:
            errors.append(f"도착지 '{end_kw}' 검색 결과가 없습니다.")
            end = None
    else:
        errors.append("도착지를 입력하세요.")
        end = None

    # 3) 경유지
    waypts = []
    for i, line in enumerate([l.strip() for l in wp_text.splitlines() if l.strip()][:5]):
        g = geocode(line)
        if g:
            waypts.append((g[0], g[1]))
            labels[f"경유{i + 1}"] = g[2]
        else:
            errors.append(f"경유지 '{line}' 검색 결과가 없습니다.")

    if errors:
        for e in errors:
            st.error(e)
    else:
        # 4) 공사 구역: 그때그때 로드 (캐시되어 두 번째부터는 즉시)
        zones = []
        if a_zn:
            try:
                items = fetch_construction()
                zones = zones_near(items, [start] + waypts + [end], radius)
            except Exception as e:
                st.warning(f"공사 데이터 로딩 실패(공사 회피 없이 진행): {e}")

        # 5) 경로 탐색
        with st.spinner("경로 탐색 중..."):
            best, cands, errs, detours = find_route(start, end, waypts, zones,
                                                     a_st, a_nr, a_zn)
        for e in errs:
            st.warning(e)
        if best:
            ss["route"] = best
            ss["labels"] = labels
            ss["zones"] = zones
            ss["cands"] = cands
            ss["detours"] = detours
            ss["opts_used"] = (a_st, a_nr, a_zn)
            st.rerun()
        else:
            st.error("경로를 찾지 못했습니다. 입력을 확인해 주세요.")

# ─────────────────────────────────────────────
# 결과 표시
# ─────────────────────────────────────────────
st.title("🚶 보행자 경로 안내")

if not ss["route"]:
    st.info("👈 사이드바에서 출발지/도착지를 입력하고 **경로 탐색**을 누르세요.\n\n"
            "주소·지명·건물명 모두 가능합니다. 출발지는 현재 위치 옵션을 쓸 수 있어요.")
    st.stop()

r = ss["route"]
pts, coords, lines, total = r["pts"], r["coords"], r["lines"], r["total"]
zones = ss["zones"]

# 지오코딩 결과 표시 (무엇으로 매칭됐는지 확인용)
with st.expander("🔎 검색된 지점 확인", expanded=False):
    for k, v in ss["labels"].items():
        st.write(f"**{k}**: {v}")

c1, c2, c3 = st.columns(3)
c1.metric("총 거리", f"{total['distance']:,} m")
c2.metric("예상 소요", f"{total['time'] // 60}분 {total['time'] % 60}초")
c3.metric("탐색 옵션", f"searchOption={r['opt']}")

dots = dotted_points(coords, spacing).assign(color="#2980b9", size=3)
layers = [dots]

# 좁은 길 구간: 보라색 촘촘한 점으로 경로 위에 겹쳐 표시
narrow_lines = [l for l in lines if is_narrow(l)]
for l in narrow_lines:
    seg = dotted_points(l.get("coords", []), max(spacing * 0.5, 4))
    if len(seg):
        layers.append(seg.assign(color="#8e44ad", size=4))

# 횡단보도 구간: 초록색 점으로 표시 (좁은 길과 별개)
cross_lines = [l for l in lines if is_crosswalk(l)]
for l in cross_lines:
    seg = dotted_points(l.get("coords", []), max(spacing * 0.4, 3))
    if len(seg):
        layers.append(seg.assign(color="#27ae60", size=5))

# 계단 안내 지점: 빨간 경고 점
stair_pts = [p for p in pts if any(k in p["desc"] for k in STAIR_KW)]
if stair_pts:
    layers.append(pd.DataFrame(
        [{"lat": p["lat"], "lon": p["lon"], "color": "#c0392b", "size": 9}
         for p in stair_pts]))

# 출발/경유/도착 마커
layers.append(pd.DataFrame([
    {"lat": p["lat"], "lon": p["lon"],
     "color": {"SP": "#2ecc71", "EP": "#e74c3c"}.get(p["type"], "#f39c12"),
     "size": 14 if p["type"] in ("SP", "EP") else 10}
    for p in pts if p["type"] in ("SP", "EP") or p["type"].startswith("PP")]))

# 공사 구역: 중심점 + 반경 원(주황)
if zones:
    ring = []
    for z in zones:
        ring.append({"lat": z["lat"], "lon": z["lon"],
                     "color": "#d35400", "size": 6})
        for deg in range(0, 360, 12):
            rad = math.radians(deg)
            ring.append({
                "lat": z["lat"] + (z["radius"] / _M) * math.sin(rad),
                "lon": z["lon"] + (z["radius"] / (_M * math.cos(
                    math.radians(z["lat"])))) * math.cos(rad),
                "color": "#e67e22", "size": 2.5})
    layers.append(pd.DataFrame(ring))

# 자동 삽입된 우회 경유지들 (청록 큰 점)
if ss["detours"]:
    layers.append(pd.DataFrame([{"lat": d[1], "lon": d[0],
                                 "color": "#16a085", "size": 12}
                                for d in ss["detours"]]))

st.map(pd.concat(layers, ignore_index=True), latitude="lat", longitude="lon",
       color="color", size="size", zoom=15)

st.caption("🔵 경로  🟢(진초록) 횡단보도  🟣 좁은 길  🔴 계단/육교/지하보도  "
           "🟠 공사 구역(원=회피 반경)  🟢 출발  🟠 경유  🔴 도착" +
           (f"  🟦(청록) 자동 우회 경유지 {len(ss['detours'])}곳"
            if ss["detours"] else ""))

# ── 회피 과정 설명 ──
a_st_u, a_nr_u, a_zn_u = ss["opts_used"]
if a_st_u or a_nr_u or a_zn_u:
    with st.expander("🧭 어떻게 회피 경로를 찾았나요? (동작 원리)", expanded=True):
        st.markdown(
            "**1단계 — 후보 경로 생성**: TMAP 보행자 API에는 공사·좁은길 회피 "
            "옵션이 없으므로, 탐색 옵션(추천 0 / 대로우선 4 / 계단제외 30)을 "
            "**여러 개 동시에 호출**해 서로 다른 후보 경로를 만듭니다.\n\n"
            "**2단계 — 위반 검사**: 각 후보 경로를 10m 간격으로 잘게 나눠, "
            "생활안전지도의 **진행 중 공사 위치**(착공일≤오늘≤준공일)와의 거리가 "
            "회피 반경 이내인 지점(공사 통과), 계단 안내 문구, 좁은 도로 구간"
            "(보행자도로·이면도로·골목, roadType 0/22)을 찾아냅니다.\n\n"
            "**3단계 — 페널티 채점**: 공사 통과 지점당 5,000점, 계단 1건당 "
            "1,000점, 좁은 길은 m당 2점, 동점이면 총거리가 짧은 쪽. "
            "**점수가 가장 낮은 후보**를 선택합니다.\n\n"
            "**4단계 — 우회 재탐색**: 최선 후보가 그래도 공사 구역을 지나면, "
            "통과 지점을 구역 중심 반대 방향으로 반경의 1.8배만큼 밀어낸 "
            "**우회 경유지**를 자동 삽입해 한 번 더 탐색합니다. "
            "지도의 청록색 점이 그 우회 경유지입니다.")
        if ss["cands"]:
            st.markdown("**후보 경로 비교** (✔ = 최종 선택)")
            st.dataframe(pd.DataFrame([{
                "선택": "✔" if c["score"] == r["score"] and c["opt"] == r["opt"] else "",
                "탐색옵션": c["opt"],
                "거리(m)": c["total"]["distance"],
                "시간(초)": c["total"]["time"],
                "계단(건)": len(c["viol"]["계단"]),
                "좁은길(구간)": len(c["viol"]["좁은길"]),
                "공사통과(지점)": len(c["hits"]),
                "페널티": c["score"],
            } for c in ss["cands"]]), use_container_width=True, hide_index=True)
        if ss["detours"]:
            st.info(f"🟦 공사 구역 통과가 감지되어 우회 경유지 "
                    f"{len(ss['detours'])}곳을 자동 삽입(최대 3회 반복, "
                    f"반복마다 더 멀리 밀어냄)해 돌아가는 경로를 택했습니다.")
        if r["hits"]:
            st.error("⛔ 우회를 반복해도 공사 구역을 완전히 피하는 보행 경로를 "
                     "찾지 못했습니다. 해당 구간은 통행 불가일 수 있으니 "
                     "현장 확인이 필요합니다.")

# ── 횡단보도 목록 ──
cross_all = [l for l in lines if is_crosswalk(l)]
if cross_all:
    with st.expander(f"🟢 경로상 횡단보도 {len(cross_all)}곳"):
        for l in cross_all:
            st.write(f"・횡단보도 {l['distance']}m — {l['desc'] or l['name'] or ''}")

# ── 좁은 길 구간 목록 ──
narrow_all = [l for l in lines if is_narrow(l)]
if narrow_all:
    with st.expander(f"🟣 경로상 좁은 길 구간 {len(narrow_all)}곳 "
                     f"(총 {sum(l['distance'] for l in narrow_all)}m)"):
        for l in narrow_all:
            st.write(f"・**{l['name'] or '이름 없는 길'}** — {l['distance']}m "
                     f"(roadType={l['roadType']})")

if zones:
    with st.expander(f"🚧 경로 주변 진행중 공사 {len(zones)}건 (생활안전지도)"):
        for z in zones:
            st.write(f"・**{z['name']}** — {z['addr']} (준공 예정 {z['end']})")

v = r["viol"]
if any(v.values()):
    st.warning("⚠️ 일부 회피 조건을 만족하지 못했습니다.")
    for k, its in v.items():
        if its:
            with st.expander(f"{k} 관련 {len(its)}건"):
                for it in its:
                    st.write("・", it)
elif a_st or a_nr or a_zn:
    st.success("✅ 설정한 회피 조건을 모두 만족하는 경로입니다.")

with st.expander("📋 턴바이턴 안내"):
    for p in sorted(pts, key=lambda x: x["idx"]):
        ic = {"SP": "🟢", "EP": "🔴"}.get(
            p["type"], "🟠" if p["type"].startswith("PP") else "🔵")
        st.markdown(f"{ic} `{p['idx']:>2}` — {p['desc']}")
