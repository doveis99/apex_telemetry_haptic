# Rust 포팅 (진행 중)

파이썬 앱을 Rust로 옮기는 중. 1단계까지 끝났고 지금은 명령줄(`ats_haptics.py`에 해당)만 있음. 배포는 아직 파이썬 버전.

## 1단계: 핵심부 (완료)

| 파이썬 | Rust | 비고 |
|---|---|---|
| `effects.py` | `src/effects.rs` | 합성 전부. 출력이 파이썬과 비트 단위로 같음 (아래) |
| `apex6.py` | `src/apex6.rs` | 패킷, 인코딩, 8 ms 스트림, time-critical 스레드. RAM 읽기(`read_ram`)는 `tone.py` 진단용이라 안 옮김 |
| `telemetry.py` | `src/telemetry.rs` | 공유 메모리 읽기, 60초 시험 주행 |
| `settings.py` | `src/settings.rs` | 같은 `config.json`을 읽고 씀. 키 순서와 잘못된 값 처리도 파이썬과 같음 |
| `gamewatch.py` | `src/gamewatch.rs` | |
| `ats_haptics.py` | `src/runner.rs`, `src/bin/ats_haptics.rs` | `--scenario`, `--replay`, `--log-blocks`는 아직 없음 |
| — | `src/pymath.rs`, `src/pyrandom.rs` | 파이썬과 같은 결과를 내기 위한 것: CRT 수학 함수, `sum()`, `math.hypot`, `%`, `**`, 복소수, MT19937 |

메시지는 아직 한국어(원문)만 나옴. 번역표는 창과 함께 옮김.

## 파이썬과 같은지 확인

```
.venv\Scripts\python.exe rust\golden\make_golden.py
cd rust
cargo test
```

`make_golden.py`가 파이썬 합성기에 정해진 주행을 넣고 나온 값을 `rust/golden-data/`에 저장함 (약 400 MB, git에 안 올림): 60초 시험 주행(60/30 fps, 매 블록), 설정을 바꾼 두 가지(컨버터 변속기, 8기통, 경로 변경, 평탄 eq / 다른 eq와 세기), 시나리오 세 개, 디버그 기록 중 최근 4개의 앞 4분. `tests/parity.rs`가 같은 프레임을 Rust 합성기에 넣고 모든 샘플이 같은 float인지, 경로, 트리거 쪽, 이벤트, 페이드와 덕킹 상태까지 같은지 봄.

2026-11-08: 12개 233,500블록 모두 일치. 노이즈 시드를 하나만 바꾸면 노이즈 버스트가 있는 블록마다 어긋나서 시험이 실패하는 것도 확인함.

## 속도

`cargo run --release --example stream_timing -- 20` (패드 없이 보고서를 버리는 연결, 부하 없는 PC):

- 블록 합성: 중앙값 17.7 µs, p99 36.9 µs (파이썬 137 / 270 µs)
- 블록 간격: 중앙값 8.018 ms, 최대 8.5 ms, 9 ms 넘은 블록 없음
- `ats_haptics.exe` 하나에 712 KB (파이썬 배포 폴더 34 MB)

모든 코어가 바쁠 때(`docs/latency-notes.md`의 부하 시험)는 아직 재지 않음.

## 남은 단계

2. `replay.py`, `scenarios.py`, `blocklog.py` (기록 형식은 그대로 두어 파이썬 분석 도구가 계속 읽게)
3. 창(GUI), 효과 테스트(`preview.py`), 모터 보정, 플러그인 설치, 업데이트, 언어 9개
4. `build.ps1`을 cargo 빌드로
