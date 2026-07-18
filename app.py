"""
TMAP 보행자 경로안내 - 지도 클릭 기반 + 계단/공사/좁은길 회피
================================================================
POST https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1

[중요] API가 지원하는 searchOption 은 4가지뿐입니다.
    0  : 추천 (기본값)
    4  : 추천 + 대로우선   <- '좁은 길 회피'의 근사치
    10 : 최단
    30 : 최단거리 + 계단제외  <- 유일한 공식 계단 제외 옵션
'공사 구간 제외', '좁은 길 제외'는 API 파라미터가 없으므로
  (1) 여러 searchOption 을 동시 호출해 후보 경로 생성
  (2) 응답을 파싱해 계단/좁은길/공사구역 위반을 페널티로 점수화
  (3) 최저 페널티 경로 선택, 위반 시 회피 경유지를 넣어 재탐색
방식으로 구현했습니다.

설치:
    pip install streamlit requests folium streamlit-folium streamlit-geolocation
실행:
    streamlit run tmap_walk_app.py
"""

import json
import math
from urllib.parse import quote, unquote

import folium
import pandas as pd
import requests
import streamlit as st
from folium.plugins import Draw, LocateControl
from streamlit_folium import st_folium

API_URL = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1"

SEARCH_OPTIONS = {
    0: "추천",
    4: "추천+대로우선",
    10: "최단",
    30: "최단거리+계단제외",
}

# 후처리 필터 기준 (환경에 맞게 조정 가능)
STAIR_KEYWORDS = ["계단", "육교", "지하보도", "에스컬레이터"]
NARROW_KEYWORDS = ["보행자도로", "이면도로", "골목"]
NARROW_ROADTYPES = {0, 22}          # 미분류 / 보행자전용 구간
CONSTRUCTION_KEYWORDS = ["공사", "통제", "폐쇄", "우회"]

from pathlib import Path

IMG_PATH = Path(__file__).parent / "image.png"

st.set_page_config(
    page_title="보행자 경로안내",
    page_icon=str(IMG_PATH) if IMG_PATH.exists() else "🚶",
    layout="wide",
)


# ══════════════════════════════════════════════════════════
# 1. API 호출
# ══════════════════════════════════════════════════════════
def call_pedestrian_api(app_key, start, end, search_option=0,
                        pass_list=None, start_name="출발", end_name="도착"):
    """start/end = (lon, lat).  pass_list = [(lon, lat), ...] 최대 5곳"""
    body = {
        "startX": start[0], "startY": start[1],
        "endX": end[0], "endY": end[1],
        "reqCoordType": "WGS84GEO",
        "resCoordType": "WGS84GEO",
        "startName": quote(start_name),      # UTF-8 URL 인코딩 필수
        "endName": quote(end_name),
        "searchOption": str(search_option),
        "sort": "index",
    }
    if pass_list:
        body["passList"] = "_".join(f"{lo:.7f},{la:.7f}" for lo, la in pass_list[:5])

    r = requests.post(
        API_URL,
        headers={"appKey": app_key, "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ══════════════════════════════════════════════════════════
# 2. 파싱 & 위반 검사
# ══════════════════════════════════════════════════════════
def clean(t):
    try:
        return unquote(t) if t else ""
    except Exception:
        return t or ""


def parse_route(gj):
    pts, lines, total = [], [], {"distance": 0, "time": 0}
    for f in gj.get("features", []):
        g, p = f["geometry"], f["properties"]
        if g["type"] == "Point":
            lon, lat = g["coordinates"]
            pts.append({
                "lat": lat, "lon": lon,
                "idx": p.get("pointIndex", 0),
                "type": p.get("pointType", ""),
                "name": clean(p.get("name")) or clean(p.get("nearPoiName")),
                "desc": clean(p.get("description")),
                "turnType": p.get("turnType"),
            })
            if p.get("pointType") == "SP":
                total["distance"] = p.get("totalDistance", 0)
                total["time"] = p.get("totalTime", 0)
        else:
            lines.append({
                "coords": g["coordinates"],
                "name": p.get("name", "") or "",
                "distance": p.get("distance", 0),
                "time": p.get("time", 0),
                "roadType": p.get("roadType", -1),
                "facilityType": str(p.get("facilityType", "")),
                "desc": clean(p.get("description")),
            })
    return pts, lines, total


# ── 기하 유틸 (shapely 없이) ────────────────────────────
def haversine_m(a, b):
    """a, b = (lon, lat)"""
    R = 6371000.0
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp = p2 - p1
    dl = math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def point_in_polygon(pt, poly):
    """ray casting. pt=(lon,lat), poly=[(lon,lat), ...]"""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-15) + x1
            if x < xin:
                inside = not inside
    return inside


def in_avoid_zone(pt, zones):
    for z in zones:
        if z["kind"] == "circle":
            if haversine_m(pt, z["center"]) <= z["radius"]:
                return True
        elif point_in_polygon(pt, z["poly"]):
            return True
    return False


# ── 위반 점수화 ────────────────────────────────────────
def evaluate(pts, lines, total, zones, avoid_stairs, avoid_narrow, avoid_zone):
    """페널티 점수 + 위반 상세 반환. 점수 낮을수록 좋음."""
    v = {"계단": [], "좁은길": [], "공사": []}

    if avoid_stairs:
        for p in pts:
            if any(k in p["desc"] for k in STAIR_KEYWORDS):
                v["계단"].append(p["desc"])
        for l in lines:
            if any(k in (l["name"] + l["desc"]) for k in STAIR_KEYWORDS):
                v["계단"].append(f"{l['name']} {l['distance']}m")

    narrow_m = 0
    if avoid_narrow:
        for l in lines:
            if l["roadType"] in NARROW_ROADTYPES or \
               any(k in l["name"] for k in NARROW_KEYWORDS):
                narrow_m += l["distance"]
                v["좁은길"].append(f"{l['name'] or '미분류'} {l['distance']}m "
                                  f"(roadType={l['roadType']})")

    hits = []
    if avoid_zone and zones:
        for l in lines:
            for c in l["coords"]:
                if in_avoid_zone(tuple(c), zones):
                    hits.append(tuple(c))
                    v["공사"].append(f"{l['name'] or '구간'} 통과")
                    break
        for p in pts:
            if any(k in p["desc"] for k in CONSTRUCTION_KEYWORDS):
                v["공사"].append(p["desc"])

    score = (
        len(v["계단"]) * 1000          # 계단은 사실상 금지
        + len(v["공사"]) * 5000        # 공사 구간은 절대 회피
        + narrow_m * 2                 # 좁은 길은 거리 비례 페널티
        + total["distance"]            # 동점이면 짧은 경로
    )
    return score, v, hits


# ── 공사 구역 회피용 경유지 생성 ────────────────────────
def make_detour(hits, zones):
    """위반 좌표를 구역 밖으로 밀어낸 경유지 1개 생성"""
    if not hits:
        return None
    lon, lat = hits[len(hits) // 2]
    best = min(zones, key=lambda z: haversine_m(
        (lon, lat), z["center"] if z["kind"] == "circle"
        else (sum(p[0] for p in z["poly"]) / len(z["poly"]),
              sum(p[1] for p in z["poly"]) / len(z["poly"]))))
    cx, cy = (best["center"] if best["kind"] == "circle"
              else (sum(p[0] for p in best["poly"]) / len(best["poly"]),
                    sum(p[1] for p in best["poly"]) / len(best["poly"])))
    r = best.get("radius", 60)
    dx, dy = lon - cx, lat - cy
    norm = math.hypot(dx, dy) or 1e-9
    push = (r * 1.8) / 111320.0        # m -> deg 근사
    return (cx + dx / norm * push, cy + dy / norm * push)


# ══════════════════════════════════════════════════════════
# 3. 탐색 오케스트레이션
# ══════════════════════════════════════════════════════════
def find_best_route(app_key, start, end, zones,
                    avoid_stairs, avoid_narrow, avoid_zone):
    # 회피 조건에 맞는 searchOption 우선순위 구성
    opts = [30, 4, 0, 10] if avoid_stairs else ([4, 0, 30, 10] if avoid_narrow else [0, 4, 10, 30])

    candidates, errors = [], []
    for so in opts:
        try:
            gj = call_pedestrian_api(app_key, start, end, so)
        except Exception as e:
            errors.append(f"searchOption={so}: {e}")
            continue
        pts, lines, total = parse_route(gj)
        sc, v, hits = evaluate(pts, lines, total, zones,
                               avoid_stairs, avoid_narrow, avoid_zone)
        candidates.append({"opt": so, "gj": gj, "pts": pts, "lines": lines,
                           "total": total, "score": sc, "viol": v, "hits": hits})

    if not candidates:
        raise RuntimeError("모든 요청 실패:\n" + "\n".join(errors))

    best = min(candidates, key=lambda c: c["score"])

    # 공사 구역을 여전히 통과하면 회피 경유지 삽입 후 1회 재탐색
    if avoid_zone and best["hits"]:
        wp = make_detour(best["hits"], zones)
        if wp:
            try:
                gj = call_pedestrian_api(app_key, start, end, best["opt"],
                                         pass_list=[wp])
                pts, lines, total = parse_route(gj)
                sc, v, hits = evaluate(pts, lines, total, zones,
                                       avoid_stairs, avoid_narrow, avoid_zone)
                if sc < best["score"]:
                    best = {"opt": best["opt"], "gj": gj, "pts": pts, "lines": lines,
                            "total": total, "score": sc, "viol": v, "hits": hits,
                            "detour": wp}
            except Exception as e:
                errors.append(f"우회 재탐색 실패: {e}")

    return best, candidates, errors


# ══════════════════════════════════════════════════════════
# 4. UI
# ══════════════════════════════════════════════════════════
ss = st.session_state
ss.setdefault("start", None)
ss.setdefault("end", None)
ss.setdefault("mode", "출발지")
ss.setdefault("zones", [])
ss.setdefault("result", None)

# ── 헤더: 썸네일 이미지 + 제목 ──────────────────────
if IMG_PATH.exists():
    h1, h2 = st.columns([1, 9], vertical_alignment="center")
    with h1:
        st.image(str(IMG_PATH), width=90)
    with h2:
        st.title("보행자 경로안내 — 계단·공사·좁은길 회피")
    st.logo(str(IMG_PATH))          # 사이드바 상단 로고
else:
    st.title("🚶 보행자 경로안내 — 계단·공사·좁은길 회피")
    st.caption("app.py와 같은 폴더에 image.png를 넣으면 로고가 표시됩니다.")

with st.sidebar:
    st.header("① 인증")
    app_key = st.text_input("TMAP appKey", type="password",
                            value=st.secrets.get("TMAP_APP_KEY", ""))

    st.header("② 지점 선택")
    ss["mode"] = st.radio("지도 클릭 시 지정할 지점",
                          ["출발지", "도착지"], horizontal=True)

    try:
        from streamlit_geolocation import streamlit_geolocation
        st.caption("현재 위치를 출발지로:")
        loc = streamlit_geolocation()
        if loc and loc.get("latitude"):
            if st.button("📍 현재 위치를 출발지로 설정"):
                ss["start"] = (loc["longitude"], loc["latitude"])
                st.rerun()
    except ImportError:
        st.caption("`pip install streamlit-geolocation` 시 현재 위치 버튼 활성화")

    c1, c2 = st.columns(2)
    if c1.button("출발지 초기화"):
        ss["start"] = None
    if c2.button("도착지 초기화"):
        ss["end"] = None

    st.header("③ 회피 조건")
    avoid_stairs = st.checkbox("계단 제외", True)
    avoid_narrow = st.checkbox("좁은 길 제외 (대로 우선)", True)
    avoid_zone = st.checkbox("공사 구역 제외", True)
    st.caption("공사 구역은 지도의 그리기 도구(원/다각형)로 직접 지정하세요. "
               "TMAP API는 공사 정보를 제공하지 않습니다.")

    if ss["zones"]:
        st.write(f"등록된 회피 구역: {len(ss['zones'])}개")
        if st.button("회피 구역 모두 삭제"):
            ss["zones"] = []
            st.rerun()

# ── 지도 ──────────────────────────────────────────────
center = ss["start"] or ss["end"] or (126.9236, 37.5568)
m = folium.Map(location=[center[1], center[0]], zoom_start=16, tiles="CartoDB positron")
LocateControl(auto_start=False).add_to(m)
Draw(export=False,
     draw_options={"polyline": False, "marker": False, "circlemarker": False,
                   "rectangle": True, "polygon": True, "circle": True},
     edit_options={"edit": False}).add_to(m)

if ss["start"]:
    folium.Marker([ss["start"][1], ss["start"][0]], tooltip="출발",
                  icon=folium.Icon(color="green", icon="play")).add_to(m)
if ss["end"]:
    folium.Marker([ss["end"][1], ss["end"][0]], tooltip="도착",
                  icon=folium.Icon(color="red", icon="stop")).add_to(m)

for z in ss["zones"]:
    if z["kind"] == "circle":
        folium.Circle([z["center"][1], z["center"][0]], radius=z["radius"],
                      color="#e67e22", fill=True, fill_opacity=0.25,
                      tooltip="회피 구역").add_to(m)
    else:
        folium.Polygon([[p[1], p[0]] for p in z["poly"]],
                       color="#e67e22", fill=True, fill_opacity=0.25,
                       tooltip="회피 구역").add_to(m)

res = ss["result"]
if res:
    for l in res["lines"]:
        folium.PolyLine([[c[1], c[0]] for c in l["coords"]],
                        color="#2980b9", weight=6, opacity=0.85,
                        tooltip=f"{l['name'] or '구간'} {l['distance']}m").add_to(m)
    for p in res["pts"]:
        if p["type"] in ("SP", "EP"):
            continue
        folium.CircleMarker([p["lat"], p["lon"]], radius=4, color="#2c3e50",
                            fill=True, fill_opacity=1,
                            tooltip=p["desc"]).add_to(m)
    if res.get("detour"):
        folium.Marker([res["detour"][1], res["detour"][0]], tooltip="우회 경유지",
                      icon=folium.Icon(color="orange", icon="share-alt")).add_to(m)

map_state = st_folium(m, width=None, height=560,
                      returned_objects=["last_clicked", "all_drawings"])

# 클릭 → 출발/도착 지정
if map_state and map_state.get("last_clicked"):
    lc = map_state["last_clicked"]
    pt = (lc["lng"], lc["lat"])
    key = "start" if ss["mode"] == "출발지" else "end"
    if ss[key] != pt:
        ss[key] = pt
        st.rerun()

# 그리기 → 회피 구역 등록
if map_state and map_state.get("all_drawings"):
    zones = []
    for d in map_state["all_drawings"]:
        props, geom = d.get("properties", {}), d["geometry"]
        if geom["type"] == "Point" and "radius" in props:
            zones.append({"kind": "circle",
                          "center": tuple(geom["coordinates"]),
                          "radius": float(props["radius"])})
        elif geom["type"] == "Polygon":
            zones.append({"kind": "poly",
                          "poly": [tuple(c) for c in geom["coordinates"][0]]})
    if zones != ss["zones"]:
        ss["zones"] = zones
        st.rerun()

# ── 실행 ──────────────────────────────────────────────
ready = bool(app_key and ss["start"] and ss["end"])
if st.button("🔍 경로 탐색", type="primary", disabled=not ready, use_container_width=True):
    with st.spinner("여러 탐색 옵션으로 후보 경로를 비교하는 중..."):
        try:
            best, cands, errs = find_best_route(
                app_key, ss["start"], ss["end"], ss["zones"],
                avoid_stairs, avoid_narrow, avoid_zone)
            ss["result"] = best
            ss["cands"] = cands
            ss["errs"] = errs
            st.rerun()
        except Exception as e:
            st.error(f"탐색 실패: {e}")

if not ready:
    need = []
    if not app_key: need.append("appKey")
    if not ss["start"]: need.append("출발지")
    if not ss["end"]: need.append("도착지")
    st.info("필요: " + ", ".join(need) + " — 지도를 클릭해 지점을 지정하세요.")

# ── 결과 ──────────────────────────────────────────────
if res:
    st.divider()
    a, b, c = st.columns(3)
    a.metric("총 거리", f"{res['total']['distance']:,} m")
    b.metric("예상 소요", f"{res['total']['time']//60}분 {res['total']['time']%60}초")
    c.metric("탐색 옵션", SEARCH_OPTIONS[res["opt"]])

    v = res["viol"]
    if any(v.values()):
        st.warning("⚠️ 회피 조건을 완전히 만족하는 경로를 찾지 못했습니다.")
        for k, items in v.items():
            if items:
                with st.expander(f"{k} 관련 구간 {len(items)}건"):
                    for i in items:
                        st.write("・", i)
    else:
        st.success("✅ 설정한 회피 조건을 모두 만족하는 경로입니다.")

    with st.expander("후보 경로 비교"):
        st.dataframe(pd.DataFrame([{
            "탐색옵션": SEARCH_OPTIONS[c["opt"]],
            "거리(m)": c["total"]["distance"],
            "시간(초)": c["total"]["time"],
            "계단": len(c["viol"]["계단"]),
            "좁은길": len(c["viol"]["좁은길"]),
            "공사": len(c["viol"]["공사"]),
            "페널티": c["score"],
        } for c in ss.get("cands", [])]), use_container_width=True)

    st.subheader("📋 상세 안내")
    for p in sorted(res["pts"], key=lambda x: x["idx"]):
        icon = {"SP": "🟢", "EP": "🔴"}.get(p["type"], "🟠" if p["type"].startswith("PP") else "🔵")
        nm = f" · **{p['name']}**" if p["name"] else ""
        st.markdown(f"{icon} `{p['idx']:>2}`{nm} — {p['desc']}")
