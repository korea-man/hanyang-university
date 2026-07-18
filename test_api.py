"""
TMAP 보행자 API 자가진단 스크립트
================================
앱을 띄우기 전에 이것부터 실행하세요:
    python test_api.py

검증 항목:
  1. appKey 유효성 (4개 searchOption 모두 호출)
  2. 응답 파싱 (app.py의 parse_route 재사용)
  3. roadType 실측 분포  -> NARROW_ROADTYPES 상수 보정 근거
  4. 계단/좁은길 키워드 매칭 실측
  5. 경유지(passList) 동작 확인
"""
import json
import sys
from collections import Counter
from pathlib import Path

import requests

# app.py의 파싱/평가 로직 재사용
sys.path.insert(0, str(Path(__file__).parent))
from tmap_core import (API_URL, SEARCH_OPTIONS, STAIR_KEYWORDS, NARROW_KEYWORDS,
                 NARROW_ROADTYPES, call_pedestrian_api, parse_route)

# ── appKey 로드: secrets.toml -> 환경변수 -> 직접입력 순 ──
def load_key():
    import os, re
    p = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if p.exists():
        m = re.search(r'TMAP_APP_KEY\s*=\s*"([^"]+)"', p.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    if os.environ.get("TMAP_APP_KEY"):
        return os.environ["TMAP_APP_KEY"]
    return input("TMAP appKey 입력: ").strip()

KEY = load_key()

# 홍대 인근 테스트 좌표 (문서 예제와 동일)
START = (126.92365493654832, 37.556770374096615)
END = (126.92432158129688, 37.55279861528311)
WAYPOINTS = [(126.92774822, 37.55395475), (126.92577620, 37.55337145)]

ok = fail = 0

def check(label, fn):
    global ok, fail
    try:
        fn()
        ok += 1
        print(f"  ✅ {label}")
    except Exception as e:
        fail += 1
        print(f"  ❌ {label}: {e}")

print("=" * 60)
print("1) searchOption 4종 호출 + roadType 실측")
print("=" * 60)
road_counter = Counter()
results = {}
for so, name in SEARCH_OPTIONS.items():
    def run(so=so):
        gj = call_pedestrian_api(KEY, START, END, so)
        pts, lines, total = parse_route(gj)
        assert total["distance"] > 0, "totalDistance=0"
        results[so] = (pts, lines, total)
        for l in lines:
            road_counter[(l["roadType"], l["name"] or "-")] += 1
    check(f"searchOption={so} ({name})", run)

if results:
    print("\n[searchOption별 비교]")
    print(f"{'옵션':<22}{'거리(m)':>8}{'시간(초)':>9}{'구간수':>6}")
    for so, (pts, lines, total) in results.items():
        print(f"{SEARCH_OPTIONS[so]:<22}{total['distance']:>8}{total['time']:>9}{len(lines):>6}")

    print("\n[roadType 실측 분포]  ← NARROW_ROADTYPES 보정 근거")
    for (rt, nm), cnt in sorted(road_counter.items()):
        flag = " <== 현재 '좁은길' 판정" if rt in NARROW_ROADTYPES else ""
        print(f"  roadType={rt:<4} 도로명={nm:<12} x{cnt}{flag}")

    print("\n[계단 키워드 매칭 실측]")
    hits = 0
    for so, (pts, lines, total) in results.items():
        for p in pts:
            for k in STAIR_KEYWORDS:
                if k in p["desc"]:
                    hits += 1
                    print(f"  옵션{so}: '{k}' in \"{p['desc']}\"")
    if not hits:
        print("  (이 구간엔 계단 안내 없음 — 계단 많은 구간으로 좌표를 바꿔 재확인 권장)")
    # searchOption=30(계단제외)과 0(추천)의 계단 건수 비교가 핵심 검증
    def stair_count(so):
        pts, lines, _ = results.get(so, ([], [], {}))
        return sum(1 for p in pts for k in STAIR_KEYWORDS if k in p["desc"])
    if 0 in results and 30 in results:
        print(f"  계단 건수: 추천(0)={stair_count(0)} vs 계단제외(30)={stair_count(30)}"
              f"  {'✅ 제외 효과 확인' if stair_count(30) <= stair_count(0) else '⚠️ 확인 필요'}")

print("\n" + "=" * 60)
print("2) 경유지(passList) 동작")
print("=" * 60)
def run_wp():
    gj = call_pedestrian_api(KEY, START, END, 0, pass_list=WAYPOINTS)
    pts, lines, total = parse_route(gj)
    pp = [p for p in pts if p["type"].startswith("PP")]
    assert len(pp) == len(WAYPOINTS), f"경유지 {len(WAYPOINTS)}곳 요청, {len(pp)}곳 응답"
    print(f"  경유지 {len(pp)}곳 모두 경로에 반영 / 총거리 {total['distance']}m")
check("경유지 2곳 경로", run_wp)

print("\n" + "=" * 60)
print(f"결과: {ok}건 통과 / {fail}건 실패")
print("=" * 60)
if fail == 0:
    print("모든 검증 통과 — streamlit run app.py 로 앱을 실행하세요.")
else:
    print("실패 항목의 오류 메시지를 확인하세요. 403이면 appKey, "
          "429면 일일 호출량 초과입니다.")
