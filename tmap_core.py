"""
tmap_core.py — TMAP 보행자 경로안내 핵심 로직 (UI 무관)
app.py(Streamlit UI)와 test_api.py(자가진단)가 공용으로 사용
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

import pandas as pd
import requests

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


# ── 우회 경유지를 경유지 목록의 올바른 위치에 삽입 ──────
def insert_detour(waypoints, detour, start, end):
    """start→wp1→...→wpN→end 앵커 시퀀스 중 detour와 가장 가까운
    구간(연속 앵커 쌍)의 사이에 삽입. passList 5곳 제한 준수."""
    if len(waypoints) >= 5:
        return None                      # 자리가 없으면 삽입 불가
    anchors = [start] + list(waypoints) + [end]
    best_i, best_d = 0, float("inf")
    for i in range(len(anchors) - 1):
        mid = ((anchors[i][0] + anchors[i + 1][0]) / 2,
               (anchors[i][1] + anchors[i + 1][1]) / 2)
        d = haversine_m(detour, mid)
        if d < best_d:
            best_i, best_d = i, d
    out = list(waypoints)
    out.insert(best_i, detour)           # anchors[i]와 [i+1] 사이 = waypoints의 i 위치
    return out


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
                    avoid_stairs, avoid_narrow, avoid_zone,
                    waypoints=None):
    waypoints = waypoints or []
    # 회피 조건에 맞는 searchOption 우선순위 구성
    opts = [30, 4, 0, 10] if avoid_stairs else ([4, 0, 30, 10] if avoid_narrow else [0, 4, 10, 30])

    candidates, errors = [], []
    for so in opts:
        try:
            gj = call_pedestrian_api(app_key, start, end, so,
                                     pass_list=waypoints or None)
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
        merged = insert_detour(waypoints, wp, start, end) if wp else None
        if merged:
            try:
                gj = call_pedestrian_api(app_key, start, end, best["opt"],
                                         pass_list=merged)
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


