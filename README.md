# ATS 햅틱 for Flydigi APEX 6 Pro

American Truck Simulator / Euro Truck Simulator 2 텔레메트리로 Flydigi APEX 6 Pro의 VCM(보이스 코일) 모터를 직접 구동하는 도구입니다. 게임 소리를 분석하는 X-Haptics와 달리, 엔진 rpm·노면·서스펜션·페달 같은 게임 데이터로 진동을 합성합니다.

## 동작 구조

```
ATS ─(scs-telemetry 플러그인, 공유 메모리)─▶ telemetry.py ─▶ effects.py (1 kHz 합성) ─▶ apex6.py ─(HID 0x57, 8 ms)─▶ APEX 6 Pro
```

- **텔레메트리:** RenCloud scs-sdk-plugin 계열(rev 12) 공유 메모리를 읽습니다. SimHub(`Local\SHSCSTelemetry`)나 TruckDash(`Local\TruckDashTelemetry`)가 설치한 플러그인을 그대로 사용하며, 별도 플러그인 설치는 필요 없습니다.
- **출력:** 8 ms마다 33바이트 HID 리포트 하나에 1 kHz 샘플 8개(트리거, 왼쪽 그립, 오른쪽 그립)를 담아 보냅니다.
- **채널:** 그립은 좌우 독립입니다. 트리거 채널은 이 펌웨어에서 좌우 라우팅이 무시되어 항상 양쪽 트리거가 함께 울립니다.

## 효과

| 구분 | 효과 |
|---|---|
| 주행 | 엔진(점화 주파수), 노면 질감(재질·속도), 요철/포트홀(바퀴별 좌우), 변속, 리타더/엔진브레이크, 충돌, 트리거(엑셀: 엔진 맥동, 브레이크: 휠락 펄스) |
| 이벤트 | 페달 바닥, 브레이크 스퀼, 트레일러 연결/분리, 파킹 브레이크, 방향지시등, 시동 켜기/끄기, 레인지/스플리터, 게임 알림(톨게이트·벌금·주유 등) |

## 설치

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python 3.12, Windows에서 테스트했습니다. 컨트롤러는 유선 또는 2.4 GHz 동글로 연결합니다.

## 사용

실행 전에 **xHaptics Engine을 끄세요.** 같은 모터에 출력해서 섞입니다.

- **튜닝 UI:** `start_gui.cmd` 더블클릭 (또는 `.venv\Scripts\pythonw.exe gui.py`). 슬라이더는 주행 중 즉시 반영되고, "저장"하면 `config.json`에 기록됩니다.
- **명령줄:** `python ats_haptics.py --monitor` (게임 대기 후 자동 연결), `--simulate`로 게임 없이 40초 시나리오 주행.
- **진단:** `python tone.py probe | tone | sweep | walk | route-test | diag`

## 출처

- APEX 6 Pro HID 프로토콜(`0x53`/`0x57`, 패킷 형식): [ApexSenseBridge](https://github.com/ReynArts/ApexSenseBridge) (MIT) 소스를 참고해 Python으로 옮겼습니다.
- 텔레메트리 메모리 배치: [RenCloud/scs-sdk-plugin](https://github.com/RenCloud/scs-sdk-plugin) V.1.12, [truck-telemetry](https://pypi.org/project/truck-telemetry/)의 구조 정의.

비공식 리버스 엔지니어링 기반이며 Flydigi나 SCS Software와 관계가 없습니다. 펌웨어 업데이트로 동작이 바뀔 수 있습니다.
