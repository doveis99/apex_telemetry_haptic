# Rust 포팅 (진행 중)

파이썬 앱을 Rust로 옮기는 중. 2단계까지 끝났고 지금은 명령줄(`ats_haptics.py`, `replay.py`에 해당)만 있음. 배포는 아직 파이썬 버전.

## 옮긴 것

| 파이썬 | Rust | 비고 |
|---|---|---|
| `effects.py` | `src/effects.rs` | 합성 전부. 출력이 파이썬과 비트 단위로 같음 (아래) |
| `apex6.py` | `src/apex6.rs` | 패킷, 인코딩, 8 ms 스트림, time-critical 스레드. RAM 읽기(`read_ram`)는 `tone.py` 진단용이라 안 옮김 |
| `telemetry.py` | `src/telemetry.rs` | 공유 메모리 읽기, 60초 시험 주행 |
| `settings.py` | `src/settings.rs` | 같은 `config.json`을 읽고 씀. 키 순서, 잘못된 값 처리, CRLF 줄끝까지 파이썬과 같음 |
| `gamewatch.py` | `src/gamewatch.rs` | |
| `scenarios.py` | `src/scenarios.rs` | 고속도로, 도시, 산길 |
| `blocklog.py` | `src/blocklog.rs` | 열과 서식이 글자 단위로 같음. `.json`의 version은 `0.19.0 (rust)` |
| `replay.py` | `src/replay.rs`, `src/bin/replay.rs` | 0.16.0 전 기록도 읽음. 그보다 오래된 13열 기록은 파이썬도 못 읽음 |
| `ats_haptics.py` | `src/runner.rs`, `src/bin/ats_haptics.rs` | `--scenario`, `--replay/--from/--to`, `--log-blocks` 포함 |
| — | `src/pymath.rs`, `src/pyrandom.rs` | 파이썬과 같은 결과를 내기 위한 것: CRT 수학 함수, `sum()`, `math.hypot`, `%`, `**`, 복소수, MT19937 |

메시지는 아직 한국어(원문)만 나옴. 번역표는 창과 함께 옮김.

## 파이썬과 같은지 확인

```
.venv\Scripts\python.exe rust\golden\make_golden.py
cd rust
cargo test
```

`make_golden.py`가 파이썬 코드에 정해진 주행을 넣고 나온 값을 `rust/golden-data/`에 저장함 (약 450 MB, git에 안 올림). `tests/parity.rs`가 Rust 쪽과 비교함.

| 확인하는 것 | 주행 | 기준 |
|---|---|---|
| 합성기 출력 | 60초 시험 주행(60/30 fps, 매 블록), 설정을 바꾼 두 가지(컨버터 변속기, 8기통, 경로 변경, 평탄 eq / 다른 eq와 세기), 시나리오 세 개, 디버그 기록 6개(0.16.0 전 형식 2개 포함)의 앞 4분 | 모든 샘플이 같은 float. 경로, 트리거 쪽, 이벤트, 페이드와 덕킹 상태도 |
| 프레임 원천 | 시험 주행, 시나리오, 기록 읽기 | 프레임이 모든 값까지 같음 |
| 디버그 기록 | 시험 주행 두 가지, 도시 시나리오, 기록 2개를 다시 기록 | 시각과 간격 열을 뺀 모든 줄이 바이트 단위로 같음. 옆의 `.json`도 |
| replay 요약 | 기록 6개 | 출력 줄(0.5 넘는 노크 목록 포함)이 같음 |
| `config.json` | 기본값 저장 후 언어 저장 | 파일이 바이트 단위로 같음 (`tests/data/python_config.json`) |

2026-11-08: 모두 일치. 합성기 14개 268,033블록, 기록 줄 95,500개. 노이즈 시드를 하나만 바꾸면 노이즈 버스트가 있는 블록마다 어긋나서 시험이 실패하는 것도 확인함.

Rust가 쓴 기록은 파이썬 `replay.py`도 그대로 읽음 (같은 요약이 나옴).

## 속도

`cargo run --release --example stream_timing -- 20` (패드 없이 보고서를 버리는 연결, 부하 없는 PC):

- 블록 합성: 중앙값 17.7 µs, p99 36.9 µs (파이썬 137 / 270 µs)
- 블록 간격: 중앙값 8.018 ms, 최대 8.5 ms, 9 ms 넘은 블록 없음
- 40분 주행 기록 replay: 4.6초 (파이썬 55초)
- `ats_haptics.exe` 하나에 712 KB (파이썬 배포 폴더 34 MB)

모든 코어가 바쁠 때(`docs/latency-notes.md`의 부하 시험)는 아직 재지 않음.

## 남은 단계

3. 창(GUI), 효과 테스트(`preview.py`), 모터 보정, 플러그인 설치, 업데이트, 언어 9개
4. `build.ps1`을 cargo 빌드로
