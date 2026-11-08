# Rust 포팅

파이썬 앱을 Rust로 옮겼음: 창(`ATSHaptics.exe`), 명령줄(`ats_haptics.exe`), 기록 요약(`replay.exe`). 0.20.0부터 배포 zip은 이 코드로 만듦 ([배포 빌드](#배포-빌드)). `python/`의 파이썬 파일은 비교 기준과 패드 진단(`tone.py`)용으로 남아 있음. 실제 패드로는 아직 써 보지 않았음.

## 폴더

처음엔 `rust/` 안에서 옮겼고, 0.20.0 배포 전에 Rust 크레이트를 저장소 맨 위로 올리고 나머지를 역할별로 나눴음 (2026-11-08):

| 폴더 | 들어 있는 것 |
|---|---|
| 맨 위 | `Cargo.toml`, `Cargo.lock`, `build.rs`, `.cargo/config.toml`, `build.ps1`, README, 라이선스 |
| `src/`, `ui/`, `examples/` | Rust 앱 |
| `tests/` | `parity.rs`, `window.rs`, `data/`(바이트 단위 고정 파일), `golden-data/`(git에 안 올림) |
| `resources/` | 실행 파일에 들어가는 것: `locales/*.json`(`t!()`), `lang/*/LC_MESSAGES/*.po`(`@tr()`), `scs-sdk-plugin/`(플러그인 DLL과 라이선스) |
| `python/` | 0.19.0까지의 파이썬 구현과 그 시험(`python/tests/`), `requirements.txt`, `start_gui.cmd` |
| `tools/` | 개발 도구: `export_locales.py`, `export_gui_texts.py`, `make_golden.py`, `collect_licenses.ps1`, `pad_sound_analyze.py`, `synth_bands.py` |
| `docs/` | README 스크린샷, 메모 |

cargo는 저장소 안 어디서 실행해도 `.cargo/config.toml`을 읽음 (`rust/` 안에 있을 때는 맨 위에서 `cargo build`를 하면 VC 런타임이 정적으로 링크되지 않았음).

## 옮긴 것

아래 파이썬 파일은 모두 `python/`에 있음.

| 파이썬 | Rust | 비고 |
|---|---|---|
| `effects.py` | `src/effects.rs` | 합성 전부. 출력이 파이썬과 비트 단위로 같음 (아래) |
| `apex6.py` | `src/apex6.rs` | 패킷, 인코딩, 8 ms 스트림, time-critical 스레드. RAM 읽기(`read_ram`)는 `tone.py` 진단용이라 안 옮김 |
| `telemetry.py` | `src/telemetry.rs` | 공유 메모리 읽기, 60초 시험 주행 |
| `settings.py` | `src/settings.rs` | 같은 `config.json`을 읽고 씀. 키 순서, 잘못된 값 처리, CRLF 줄끝까지 파이썬과 같음 |
| `gamewatch.py` | `src/gamewatch.rs` | |
| `scenarios.py` | `src/scenarios.rs` | 고속도로, 도시, 산길 |
| `blocklog.py` | `src/blocklog.rs` | 열과 서식이 글자 단위로 같음. `.json`의 version은 `<버전> (rust)` |
| `replay.py` | `src/replay.rs`, `src/bin/replay.rs` | 0.16.0 전 기록도 읽음. 그보다 오래된 13열 기록은 파이썬도 못 읽음 |
| `ats_haptics.py` | `src/runner.rs`, `src/bin/ats_haptics.rs` | 명령줄. 창도 같은 `run()`을 씀 |
| `preview.py` | `src/preview.rs` | 효과 테스트(▶) |
| `calibrate.py` (+ `tone.py`의 `tone_renderer`) | `src/calibrate.rs` | 모터 보정. 패드 버튼은 XInput |
| `plugin_install.py` | `src/plugin_install.rs` | 플러그인 DLL(42 KB)은 exe 안에 들어 있음 |
| `updater.py` | `src/updater.rs` | WinHTTP와 SHA-256. 압축 풀기와 교체 스크립트는 파이썬과 같음 |
| `errorlog.py` | `src/errorlog.rs` | 패닉도 백트레이스와 함께 `error.log`에 |
| `app.py` | `src/launch.rs`, `src/bin/app/main.rs` | `--launch`(Steam), `--selftest` |
| `gui.py`, `widgets.py` | `ui/*.slint`, `src/bin/app/*.rs` | 창, 보정 창, 플러그인 창 |
| `i18n.py`, `locales/*.py` | `src/i18n.rs`, `resources/locales/*.json`, `resources/lang/*/LC_MESSAGES/*.po` | 언어 10개. 표는 `tools/export_locales.py`가 파이썬 표에서 만듦 |
| — | `src/rate2k.rs` | 2 kHz 출력(2026-11-08, `docs/xhaptics-engine-notes.md` 12절). Rust 앱에만 있음. 켜면 합성기가 틱을 따로 내놓음(`Synth::native_ticks`), 끄면 파이썬과 비트 단위로 같음 |
| — | `src/pymath.rs`, `src/pyrandom.rs` | 파이썬과 같은 결과를 내기 위한 것: CRT 수학 함수, `sum()`, `math.hypot`, `%`, `**`, 복소수, MT19937 |

## 창: Slint

게임 옆에서 가벼워야 해서 같은 시험 창(효과 22줄, 10 Hz 미터)으로 재고 골랐음 (2026-11-08, 부하 없는 PC):

| | 파이썬 tkinter | egui (OpenGL) | Slint (소프트웨어 렌더러) |
|---|---|---|---|
| private 메모리 | 29 MB | 152 MB | 13 MB |
| CPU, 미터 10 Hz | 0.3% | 1.4% | 0.8% |

실제 창(`ATSHaptics.exe --dry-run`, 정지 상태): private 18.5 MB, CPU 0.6%, exe 13.5 MB 하나.

- 배치와 색은 `gui.py`/`widgets.py` 그대로 옮김 (카드, 슬라이더, 그립·트리거·둘 다 선택, 스위치, 미터, 말풍선 도움말, 메뉴)
- 고정 글은 `.slint`의 `@tr("한국어 원문")`, 값이 섞인 글과 효과 줄은 Rust의 `t!()`. 둘 다 파이썬 표의 한국어 원문이 키
- 슬라이더 줄, 말풍선 설명, 메뉴 이름은 `tools/export_gui_texts.py`가 `gui.py`에서 그대로 뽑아 `src/bin/app/specs.rs`를 만듦
- `ATSHaptics.exe --snapshot 파일.png --language en --screen main|tip|plugins|calibration|session|result`: 창을 띄우지 않고 같은 렌더러로 그려 봄

## 파이썬과 같은지 확인

```
.venv\Scripts\python.exe tools\make_golden.py
cargo test
```

`make_golden.py`가 파이썬 코드에 정해진 주행을 넣고 나온 값을 `tests/golden-data/`에 저장함 (약 460 MB, git에 안 올림). `tests/parity.rs`가 Rust 쪽과 비교함.

| 확인하는 것 | 주행 | 기준 |
|---|---|---|
| 합성기 출력 | 60초 시험 주행(60/30 fps, 매 블록), 설정을 바꾼 두 가지(컨버터 변속기, 8기통, 경로 변경, 평탄 eq / 다른 eq와 세기), 시나리오 세 개, 디버그 기록 6개(0.16.0 전 형식 2개 포함)의 앞 4분 | 모든 샘플이 같은 float. 경로, 트리거 쪽, 이벤트, 페이드와 덕킹 상태도 |
| 효과 테스트(▶) | 효과 18개, 노면 재질 9개 각각과 전부 차례로 | 모든 샘플이 같은 float |
| 프레임 원천 | 시험 주행, 시나리오, 기록 읽기 | 프레임이 모든 값까지 같음 |
| 디버그 기록 | 시험 주행 두 가지, 도시 시나리오, 기록 2개를 다시 기록 | 시각과 간격 열을 뺀 모든 줄이 바이트 단위로 같음. 옆의 `.json`도 |
| replay 요약 | 기록 6개 | 출력 줄(0.5 넘는 노크 목록 포함)이 같음 |
| `config.json` | 기본값 저장 후 언어 저장 | 파일이 바이트 단위로 같음 (`tests/data/python_config.json`) |

그 밖의 시험:
- `tests/window.rs`: 화면 6개를 언어 10개로 모두 그려 봄 (패닉이나 `error.log`가 없어야 통과)
- `i18n` 시험: 코드의 `t!()`와 `.slint`의 `@tr()` 문장이 모두 영어 표에 있는지
- 네트워크 시험 둘(`cargo test --lib updater -- --ignored`): GitHub에서 최신 릴리스를 읽고, zip을 받아 해시를 확인하고 풀어 봄

2026-11-08: 모두 일치. 합성기 14개 268,033블록, 효과 테스트 28개, 기록 줄 95,500개. 노이즈 시드를 하나만 바꾸면 노이즈 버스트가 있는 블록마다 어긋나서 시험이 실패하는 것도 확인함.

Rust가 쓴 기록은 파이썬 `replay.py`도 그대로 읽음 (같은 요약이 나옴).

## 속도

`cargo run --release --example stream_timing -- 20` (패드 없이 보고서를 버리는 연결, 부하 없는 PC):

- 블록 합성: 중앙값 17.7 µs, p99 36.9 µs (파이썬 137 / 270 µs)
- 블록 간격: 중앙값 8.018 ms, 최대 8.5 ms, 9 ms 넘은 블록 없음
- 40분 주행 기록 replay: 4.6초 (파이썬 55초)

모든 코어가 바쁠 때(`docs/latency-notes.md`의 부하 시험)는 아직 재지 않음.

## 배포 빌드

`powershell -ExecutionPolicy Bypass -File build.ps1` (저장소 맨 위에서):

- `cargo build --release`로 `ATSHaptics.exe`(창), `ats_haptics.exe`(명령줄), `replay.exe`를 만들어 `dist\ATSHaptics\`에 모으고 `dist\ATSHaptics-<버전>.zip`으로 묶음. 플러그인 DLL은 `ATSHaptics.exe` 안에 있음
- 함께 넣는 것: README 4개, `LICENSE`, `THIRD_PARTY_NOTICES.md`, `docs\`, `source\`(git이 추적하거나 새로 생긴 파일, `target`·`tests/golden-data` 빼고), `THIRD_PARTY_LICENSES.txt`(`tools/collect_licenses.ps1`이 `Cargo.lock` 기준으로 실행 파일에 링크된 크레이트 183개의 라이선스 원문을 모음)
- `_internal\README.txt`: 파이썬 0.19 이하의 업데이터는 새 zip에서 `ATSHaptics.exe` 옆에 `_internal` 폴더가 있어야 받아들이고, 그 폴더를 `/MIR`로 덮어써 파이썬 런타임을 지움. Rust 업데이터도 같은 폴더로 설치본을 알아봄. `Compress-Archive`가 빈 폴더를 버려서 설명 파일을 넣음. `build.ps1`이 zip 안에 둘 다 있는지 마지막에 확인함
- 버전은 `Cargo.toml`의 `version` 한 곳(0.20.0부터). exe는 `CARGO_PKG_VERSION`, `build.ps1`은 zip 이름, 파이썬 `version.py`는 `tomllib`로 같은 값을 읽음
- `.cargo/config.toml`: VC 런타임(`vcruntime140.dll`)은 정적으로 링크하고 UCRT(`ucrtbase.dll`, Windows 10/11 기본)는 동적으로 둠. 파이썬 배포는 `_internal`에 `vcruntime140.dll`을 넣어 다녔기 때문에, 이게 없으면 VC++ 재배포 패키지가 없는 PC에서 업데이트 후 실행이 안 됨. 수학 함수는 파이썬과 같은 `ucrtbase.dll` 것을 그대로 써서 비트 단위 비교도 그대로 통과함

2026-11-08 확인: zip을 `Expand-Archive`로 풀어 `ATSHaptics.exe --selftest` 통과(패드 1개, ATS/ETS2 플러그인 인식, `_internal: true`). 파이썬 0.19의 `updater.py`가 하는 일을 그대로 재연(zip 풀기와 `ATSHaptics.exe`·`_internal` 확인, 가짜 0.19 설치본에 `UPDATE_SCRIPT` 실행, 마지막 `Start-Process`만 기록으로 바꿈): 받아들이고, 파이썬 런타임이 지워져 `_internal`에는 설명 파일만 남고, 새 exe 셋이 들어감.

## 아직 확인 안 된 것

- 실제 패드: 주행, 효과 테스트, 모터 보정 모두 패드 없이만 시험함
- 업데이트의 마지막 단계(설치 폴더를 바꾸고 다시 시작): Rust 업데이터는 받기, 해시, 압축 풀기까지만, 파이썬 0.19 → 0.20은 가짜 설치본에서 다시 시작 직전까지만 시험함
- 창을 사람이 직접 써 보는 것: 버튼, 메뉴, 프리셋 이름 입력, 말풍선은 그려진 모습만 확인함
