# 보행자 경로안내 (TMAP) — 계단·공사·좁은길 회피

지도 클릭으로 출발/도착을 지정하고, TMAP 보행자 경로안내 API로
계단 제외(searchOption=30), 대로 우선(4) 등 여러 옵션을 동시 비교하여
회피 조건 페널티가 가장 낮은 경로를 선택합니다.
공사 구역은 지도에 원/다각형으로 직접 그려 등록하면
해당 구역을 지나는 경로를 배제하고 우회 경유지를 넣어 재탐색합니다.

## 폴더 구성
```
tmap_route_app/
├── app.py                          # 메인 앱
├── image.png                       # 헤더 썸네일 (교체 가능, 파일명 유지)
├── requirements.txt
├── README.md
├── .gitignore
└── .streamlit/
    ├── config.toml                 # 테마
    └── secrets.toml.example        # -> secrets.toml 로 복사 후 appKey 입력
```

## 실행 방법 (로컬)
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
#   secrets.toml 에 TMAP_APP_KEY 입력 (또는 실행 후 사이드바에 직접 입력)
streamlit run app.py
```

## Streamlit Community Cloud 배포
1. 이 폴더를 GitHub 저장소로 push (secrets.toml 제외 — .gitignore 처리됨)
2. share.streamlit.io 에서 저장소 연결, Main file = `app.py`
3. App settings → Secrets 에 `TMAP_APP_KEY = "..."` 입력

## 썸네일 교체
`image.png` 를 원하는 이미지로 덮어쓰면 됩니다 (정사각형 권장, PNG).
- 페이지 상단 제목 옆 + 브라우저 탭 아이콘 + 사이드바 로고에 사용됩니다.

## 사용 순서
1. 사이드바에 appKey 입력 (secrets 설정 시 자동)
2. "출발지" 모드에서 지도 클릭 → 초록 마커 / "도착지" 모드 → 빨강 마커
   - 또는 📍 버튼으로 브라우저 현재 위치를 출발지로
3. 회피 조건 체크 (계단 / 좁은 길 / 공사 구역)
4. 공사 구역이 있으면 지도 좌측 그리기 도구로 원·다각형 표시
5. **경로 탐색** 클릭 → 후보 비교표 / 위반 내역 / 턴바이턴 안내 확인

## 주의
- TMAP API 자체는 공사·좁은길 회피 파라미터가 없어 후처리 방식으로 구현됨
  → "후보 경로 비교" 표에서 항상 결과를 확인하세요.
- `NARROW_ROADTYPES`, `STAIR_KEYWORDS` 상수는 실제 응답을 보며 조정 권장.
