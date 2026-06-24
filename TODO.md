# macstat — TODO

## 위젯 (메뉴바)
- [ ] `brew install --cask swiftbar`
- [ ] `macstat-daemon`: powermetrics 1회 띄워두고 `/tmp/macstat.json`에 주기적 스냅샷 기록
- [ ] `macstat.3s.py` SwiftBar 플러그인: JSON 읽어 메뉴바에 `🌡 pressure · NW · CPU%` 렌더, 클릭 시 상세 펼침

## Fan RPM (Apple Silicon)
- [ ] AppleSMC IOKit 호출 (`IOServiceMatching("AppleSMC")` → keys `FNum`, `F0Ac`)로 fan RPM 읽기
- Stats 프로젝트의 `SMC/smc.swift` 참고 (~700줄 Swift, 같은 패턴을 ObjC로 옮기면 ~200줄)
- 단순 shell-out 안 됨 — 헬퍼 바이너리에 SMC 모듈 추가 필요
- 우선순위 낮음 (M2 Pro MacBook 기준 thermal pressure로 대체 가능)

## 참고
- 알림센터 위젯(WidgetKit)은 갱신 주기 throttle 때문에 실시간 지표에 부적합 — 메뉴바 권장
- 실제 °C 온도가 필요해지면: `brew install macmon` 사이드카 OR Swift IOHID 헬퍼 작성
