"""
app.py — Streamlit UI (핵심 로직은 tmap_core.py)
실행: streamlit run app.py
"""
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from folium.plugins import Draw, LocateControl
from streamlit_folium import st_folium

from tmap_core import (SEARCH_OPTIONS, find_best_route)

IMG_PATH = Path(__file__).parent / "image.png"

st.set_page_config(
    page_title="보행자 경로안내",
    page_icon=str(IMG_PATH) if IMG_PATH.exists() else "🚶",
    layout="wide",
)

# ══════════════════════════════════════════════════════════
# 4. UI
# ══════════════════════════════════════════════════════════
ss = st.session_state
ss.setdefault("start", None)
ss.setdefault("end", None)
ss.setdefault("waypoints", [])     # [(lon, lat), ...] 최대 5곳
ss.setdefault("mode", "출발지")
ss.setdefault("zones", [])
ss.setdefault("result", None)

# ── 헤더: 썸네일 이미지 + 제목 ──────────────────────
if IMG_PATH.exists():
    h1, h2 = st.columns([1, 9], vertical_alignment="center")
    with h1:
        st.image(str(IMG_PATH), width=90)
    with h2:
        st.title("⛰️산 안 넘고 \n 🌉강 안 건너 온 지도")
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
                          ["출발지", "경유지", "도착지"], horizontal=True)

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

    # ── 경유지 목록 (최대 5곳, TMAP passList 제한) ──
    if ss["waypoints"]:
        st.markdown(f"**경유지 ({len(ss['waypoints'])}/5)** — 방문 순서대로")
        for i, (lo, la) in enumerate(ss["waypoints"]):
            w1, w2, w3, w4 = st.columns([4, 1, 1, 1])
            w1.caption(f"{i+1}. ({la:.5f}, {lo:.5f})")
            if w2.button("▲", key=f"wp_up_{i}", disabled=(i == 0),
                         help="순서 앞으로"):
                ss["waypoints"][i-1], ss["waypoints"][i] = \
                    ss["waypoints"][i], ss["waypoints"][i-1]
                st.rerun()
            if w3.button("▼", key=f"wp_dn_{i}",
                         disabled=(i == len(ss["waypoints"]) - 1),
                         help="순서 뒤로"):
                ss["waypoints"][i+1], ss["waypoints"][i] = \
                    ss["waypoints"][i], ss["waypoints"][i+1]
                st.rerun()
            if w4.button("✕", key=f"wp_del_{i}", help="삭제"):
                ss["waypoints"].pop(i)
                st.rerun()
        if st.button("경유지 모두 삭제"):
            ss["waypoints"] = []
            st.rerun()

    st.header("③ 회피 조건")
    avoid_stairs = st.checkbox("계단 제외", True)
    avoid_narrow = st.checkbox("좁은 길 제외 (대로 우선)", True)
    avoid_zone = st.checkbox("공사 구역 제외", True)
    st.caption("공사 구역은 지도의 그리기 도구(원/다각형)로 직접 지정하세요. "
               "TMAP API는 공사 정보를 제공하지 않습니다.")
    if avoid_zone and len(ss["waypoints"]) >= 5:
        st.warning("경유지가 이미 5곳이라 공사 구역 자동 우회 경유지를 "
                   "추가할 수 없습니다. 경유지를 줄이면 우회 재탐색이 가능합니다.")

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
for i, (lo, la) in enumerate(ss["waypoints"]):
    folium.Marker(
        [la, lo], tooltip=f"경유지 {i+1}",
        icon=folium.DivIcon(html=(
            f'<div style="background:#f39c12;color:#fff;border:2px solid #fff;'
            f'border-radius:50%;width:26px;height:26px;line-height:22px;'
            f'text-align:center;font-weight:bold;font-size:13px;'
            f'box-shadow:0 1px 4px rgba(0,0,0,.4)">{i+1}</div>'))
    ).add_to(m)

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

# 클릭 → 출발/경유/도착 지정
if map_state and map_state.get("last_clicked"):
    lc = map_state["last_clicked"]
    pt = (lc["lng"], lc["lat"])
    if ss["mode"] == "경유지":
        if pt not in ss["waypoints"]:
            if len(ss["waypoints"]) >= 5:
                st.toast("경유지는 최대 5곳입니다 (TMAP API 제한). "
                         "기존 경유지를 삭제 후 추가하세요.", icon="⚠️")
            else:
                ss["waypoints"].append(pt)
                st.rerun()
    else:
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
                avoid_stairs, avoid_narrow, avoid_zone,
                waypoints=ss["waypoints"])
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
