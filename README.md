# ATS 햅틱 for Flydigi APEX 6 Pro

American Truck Simulator / Euro Truck Simulator 2의 텔레메트리로 Flydigi APEX 6 Pro의 VCM(보이스 코일) 모터를 직접 구동합니다. 게임 소리를 분석하는 X-Haptics와 달리 엔진 rpm, 노면, 서스펜션, 페달 같은 게임 데이터로 진동을 합성합니다.

> Drives the Flydigi APEX 6 Pro voice-coil motors from ATS/ETS2 telemetry: engine, road surface, suspension bumps, gear shifts, pedals and game events, with a live tuning window. Windows only. Unofficial; not affiliated with Flydigi or SCS Software.

## 요구 사항

- Windows 10/11, Flydigi APEX 6 Pro (유선 또는 2.4 GHz 동글; 블루투스 미지원)
- American Truck Simulator 또는 Euro Truck Simulator 2
- 실행 중에는 **xHaptics Engine을 끄세요.** 같은 모터에 출력해서 섞입니다. (Space Station 웹 페이지는 닫아 두면 됩니다.)

## 설치

1. `ATSHaptics-<버전>.zip`을 원하는 곳에 압축 해제합니다.
2. `ATSHaptics\ATSHaptics.exe`를 실행합니다.
3. 처음 실행 시 게임에 호환 텔레메트리 플러그인이 없으면 **텔레메트리 플러그인** 창이 열립니다. "설치"를 누르면 게임의 `bin\win_x64\plugins`에 `scs-telemetry.dll`(RenCloud, MIT)을 복사합니다.
   - SimHub나 TruckDash가 이미 플러그인을 설치했다면 그대로 사용합니다(TruckDash: 호환, SimHub: 호환 미확인).
   - Steam이 아닌 설치나 다른 위치는 "게임 폴더 직접 선택…"으로 지정합니다.
   - 권한 오류가 나면 관리자 권한으로 실행하거나 안내된 파일을 직접 복사하세요.

## 사용

- **창에서 실행:** "ATS/ETS2 게임"을 고르고 "시작". 게임이 실행 중일 때만 컨트롤러를 사용하고, 게임이 끝나면 놓습니다.
- **게임 감지 시 자동 시작:** 켜 두면 창이 정지 상태일 때 게임이 실행되는 순간 자동으로 시작합니다(즉시 저장).
- **게임과 함께 실행 (Steam):** 게임 속성 → 실행 옵션에 아래를 넣으면 게임을 켤 때 창이 최소화 상태로 함께 뜨고, 게임을 끄면 같이 닫힙니다. 상주 프로그램은 없습니다.

  ```
  "<압축 해제 경로>\ATSHaptics\ATSHaptics.exe" --launch %command%
  ```

  기존 실행 옵션(예: `-nointro`)은 `%command%` 뒤에 붙입니다. 창이 이미 열려 있으면 두 번째 창은 뜨지 않습니다.
- **튜닝:** 슬라이더와 체크박스는 주행 중 즉시 반영됩니다. "저장"하면 `%APPDATA%\ATSHaptics\config.json`에 기록됩니다.
  - **출력 세기:** 전체 / 그립 / 트리거 최종 세기
  - **주행 효과:** 엔진, 노면, 요철, 변속, 리타더, 충돌, 엑셀/브레이크(트리거)
  - **이벤트 효과:** 페달 바닥, 브레이크 스퀼, 트레일러 연결/분리, 파킹 브레이크, 방향지시등, 시동, 레인지/스플리터, 게임 알림
  - **감지 기준:** 요철·충돌 감지 기준, 엔진 기통 수
- **테스트 주행:** 게임 없이 40초 시나리오를 컨트롤러로 재생해 효과를 확인합니다.

## 알려진 제약

- 이 펌웨어에서는 트리거 채널이 좌우 구분 없이 **양쪽 트리거를 함께** 울립니다(그립은 좌우 독립).
- 비공식 리버스 엔지니어링 기반이라 컨트롤러 펌웨어 업데이트로 동작이 바뀔 수 있습니다.

## 소스에서 실행 / 빌드

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe app.py          # 창 (또는 start_gui.cmd)
.\.venv\Scripts\python.exe ats_haptics.py --monitor   # 명령줄, --simulate / --exit-with-game
.\.venv\Scripts\python.exe tone.py probe    # 진단: probe | tone | sweep | walk | route-test | diag
powershell -ExecutionPolicy Bypass -File build.ps1   # dist\ATSHaptics-<버전>.zip
```

Steam 실행 옵션(소스): `"<repo>\.venv\Scripts\pythonw.exe" "<repo>\launch.py" %command%`

구조:

```
ATS ─(scs-telemetry 공유 메모리)─▶ telemetry.py ─▶ effects.py (1 kHz 합성) ─▶ apex6.py ─(HID 0x57, 8 ms)─▶ APEX 6 Pro
```

## 라이선스

GPL-3.0-or-later (`LICENSE`). APEX 6 프로토콜 구현은 [ApexSenseBridge](https://github.com/ReynArts/ApexSenseBridge)(GPL-3.0-or-later)를 옮긴 것이며, 동봉한 텔레메트리 플러그인은 [RenCloud/scs-sdk-plugin](https://github.com/RenCloud/scs-sdk-plugin)(MIT)입니다. 자세한 내용은 `THIRD_PARTY_NOTICES.md`를 보세요. 배포 zip에는 대응 소스가 `source\` 폴더로 함께 들어 있습니다.
