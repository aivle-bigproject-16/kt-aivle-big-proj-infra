# 🔋 KT-AIVLE-big-proj-infra

> KT AIVLE 9기 빅프로젝트 / AI 06반 16조 — 배터리 셀 CT·RGB 결함검사 생산 시뮬레이션
> **인프라(배포·컨테이너·스토리지) 레포.** PartLeader: 김경순 / 서브: 공다연·김현민

CT(파우치)·RGB(원통) 독립 검사 파이프라인 2개 구조의 **통합 뼈대**를 소유합니다. 앱 서비스(FE/BE/AI/LLM)는 각 스택 레포가 이미지를 만들고, 이 레포의 compose가 이를 배선합니다.

설계 근거 = `빅 프로젝트/파트_인프라/인프라_스켈레톤_결정_2026-07-09.md` (결정 D1~D9)

## 📦 컨테이너 구성 — 8종

| Tier | 서비스 | 프로파일 | 포트 | GPU | 역할 |
| --- | --- | --- | --- | --- | --- |
| Presentation | `frontend` | `app` | 80 | | 정적 번들 서빙 + `/api` 프록시 + `/ws` WebSocket 프록시 |
| Application | `backend` | `app` | 8080 | | 오케스트레이션·상태머신·판정·presigned·DB write·**Redis 유일 writer** |
| Application | `ai-infer` | `ai` | 8000 | ✅ | 라이브 추론(YOLOv11-seg ×2). stateless. 동기 REST |
| Application | `ai-infer-stub` | `stub` | 8000 | | GPU 없는 개발자용 고정 응답 |
| Application | `llm-report` | `app` | 8001 | | LangGraph 리포트 생성. stateless. HTTP 콜백 |
| Application | `ollama` | `ai`,`llm` | 11434 | ✅ | Qwen GGUF 서빙 |
| Data | `postgres` | *(항상)* | 5432 | | **SSOT.** 메인 RDB + 로그 JSONB |
| Data | `redis` | *(항상)* | — | | 대시보드 status 캐시 전용 |
| Data | `minio` | *(항상)* | 9000/9001 | | 개발 오브젝트 스토리지 (배포=AWS S3) |
| Data | `minio-init` | *(항상)* | — | | 버킷·프리픽스 1회 생성 후 종료 |

> **`ollama`·`redis`는 포트를 게시하지 않습니다.** 둘 다 기본 설정에 인증이 없어, 외부에 노출되면 누구나 GPU로 추론을 돌리거나 서버를 장악할 수 있습니다. 내부망(`battery-net`) 전용이며 EC2 보안그룹은 80/443만 엽니다.

## 🚀 실행합니다

```bash
cp .env.example .env      # 값 확인 후 사용 (개발 기본값 그대로 가능)
```

### 데이터 레이어만 (전원 공통)

```bash
docker compose up -d
docker compose ps                 # postgres·redis·minio = healthy 확인
docker compose logs minio-init    # 버킷 레이아웃 출력 확인
```

### FE · BE 개발자 — **GPU 불필요**

```bash
docker compose --profile app --profile stub up -d
```

`.env`에서 `AI_BASE_URL=http://ai-infer-stub:8000`으로 바꿔 두면 BE가 스텁을 호출합니다. 스텁은 아키텍처 v2 §9 계약 스키마 그대로 고정 응답을 반환하고, `STUB_LATENCY_MS`로 라이브 추론 지연을 흉내냅니다.

스텁 대신 실제 `ai-infer`를 CPU로 돌리고 싶다면 `.env`에 `DEVICE=cpu`를 두고 `--profile ai`를 켭니다. 뜨긴 뜨지만 추론이 수십 초 걸립니다.

### AI 담당 — **GPU 보유(로컬 4070 Ti)**

```bash
docker compose --profile app --profile ai up -d
```

### LLM 담당 — `ollama`만 단독

```bash
docker compose --profile llm up -d
docker compose exec ollama ollama pull qwen2.5:7b     # GPU 없으면 qwen2.5:1.5b
```

### EC2 배포 — GPU 장치 예약 추가

```bash
docker compose -f compose.yaml -f compose.gpu.yaml --profile app --profile ai up -d
```

- MinIO 콘솔: http://localhost:9001 (`.env`의 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`)
- Redis 디버깅: `docker compose exec redis redis-cli`
- 종료: `docker compose down` / 볼륨까지 초기화: `docker compose down -v`

## 🧩 프로파일 설계

**두 축을 분리했습니다.** *어떤 서비스를 띄울까*는 `profiles`가, *GPU 장치를 붙일까*는 `compose.gpu.yaml`이 담당합니다.

| 프로파일 | 서비스 | 대상 |
| --- | --- | --- |
| *(없음 = 항상 기동)* | `postgres` `redis` `minio` `minio-init` | 전원 |
| `app` | `frontend` `backend` `llm-report` | FE·BE 개발자 |
| `stub` | `ai-infer-stub` | GPU 없는 BE 개발자 |
| `ai` | `ai-infer` `ollama` | AI 담당·인프라 |
| `llm` | `ollama` | LLM 담당 (ai-infer 없이 단독) |

- **데이터 레이어에 프로파일을 주지 않습니다.** `backend`의 `depends_on: postgres`가 프로파일 경계를 넘지 않게 하기 위함입니다.
- **`backend`는 `ai-infer`·`llm-report`·`ollama`에 `depends_on`을 걸지 않습니다.** 런타임 HTTP 호출이지 기동 의존이 아닙니다. `*_BASE_URL` 환경변수로 붙습니다. 덕분에 GPU 없는 개발자도 `backend`를 띄울 수 있습니다.
- **GPU 예약(`deploy.resources.reservations.devices`)을 `compose.gpu.yaml`로 분리했습니다.** 이 블록이 `compose.yaml`에 있으면 NVIDIA 런타임이 없는 머신에서 파싱 단계부터 실패합니다.

## 🔀 서빙 모드 — 비상 스위치

```bash
SERVING_MODE=live     # ai-infer 로 라이브 추론 (기본)
SERVING_MODE=replay   # defect_result 의 과거 결과 재생
```

발표 중 GPU·AI가 죽으면 `.env`를 `replay`로 바꾸고 `docker compose up -d backend`. **FE 화면은 동일합니다.** 리허설을 라이브로 1회 완주해 DB에 결과가 쌓여 있어야 동작합니다.

## 🗂️ S3(MinIO) 버킷 구조

아키텍처 v2 §8 기준. 버킷은 **비공개**, 이미지 접근은 BE 발급 presigned URL로 일원화합니다.

```
battery/
├── pool/ct/{normal,defect}/     CT(파우치) 검증셋+증강 사전적재 (read-only, eviction 제외)
├── pool/rgb/{normal,defect}/    RGB(원통) 동일
├── defects/YYYY/MM/DD/          REJECT 이미지 보관 (BE FIFO eviction)
└── models/                      학습 가중치(.pt/.onnx) — FastAPI 기동 시 로드
```

> S3에는 실제 디렉터리가 없어 `.keep` 마커로 구조만 표시합니다. 실제 프리픽스는 boto3 `put_object` 시 생성됩니다.

## 🔌 boto3 연결 (개발=MinIO, 배포=S3 동일 코드)

```python
import boto3, os
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],      # 호스트에서: http://localhost:9000
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name=os.environ["S3_REGION"],
)
s3.list_objects_v2(Bucket=os.environ["S3_BUCKET"], Prefix="pool/ct/")
```

컨테이너 안에서는 `S3_ENDPOINT_INTERNAL`(=`http://minio:9000`)을 씁니다. 배포 시 둘 다 비우면 AWS 기본 엔드포인트로 붙습니다 — 코드 동일(아키텍처 v2 §2 결정 11).

> 증강 파이프라인(공다연)은 이 버킷 위에서 `list/get/put/copy/delete_object`로 동작합니다. 개발은 AWS 키 발급 없이 MinIO 루트 자격으로 즉시 실습 가능. 배포 전 전용 서비스 계정(최소권한)으로 분리 예정.

## 🗄️ Redis 사용 규칙

**용도는 대시보드 status 캐시 하나뿐입니다.** BE↔AI·BE↔LLM 통신에는 쓰지 않습니다.

```
키:    sim:{sessionId}:cells   (Hash)
필드:  {batteryId} → {status}   REGISTERED|CAPTURING|CAPTURED|ANALYZING|COMPLETED|FAILED
TTL:   3600s
쓰기:  BE 가 상태 전이 시 HSET. PG 커밋 이후에 쓴다(역순이면 롤백 시 유령 상태)
읽기:  HGETALL → 대시보드 현황 1회 조회
```

| # | 규칙 |
| --- | --- |
| **R1** | Redis에만 존재하는 데이터는 없다. `FLUSHALL` 해도 기능 정상(PG에서 재구성) |
| **R2** | Redis writer = **BE 단독.** AI·LLM은 Redis에 접근하지 않는다 |
| **R3** | Redis가 죽어도 BE는 산다. 연결 실패 시 PG 폴백. `depends_on` 하드 의존 금지 |
| **R4** | 모든 키에 TTL |
| **R5** | 키 네임스페이스를 문서화. 문자열을 코드에 흩뿌리지 않는다 |

## 🖥️ 배포 타깃

| 항목 | 값 |
| --- | --- |
| 인스턴스 | `g4dn.xlarge` (T4 16GB VRAM, 4 vCPU, 16GB RAM), 서울 `ap-northeast-2` |
| AMI | Deep Learning AMI (Ubuntu) — NVIDIA 드라이버·Docker·nvidia-container-toolkit 사전설치 |
| EBS | gp3 60GB |
| 기동 | **평소 중지.** 리허설·발표 구간만 |

발표 전 워밍업: `ai-infer` 모델 로드 + `ollama` 모델 상주(`OLLAMA_KEEP_ALIVE=-1`) 확인.

## 🧭 다음 Phase

- Dockerfile 템플릿(멀티스테이지) 배포 → 각 파트가 채움
- GHCR 이미지 push → compose `image:` 참조로 통합
- EC2 단일 배포 + 발표 워밍업 절차
- (P2) GitHub Actions CI/CD → (P3) EKS + ArgoCD GitOps 부분 PoC · 관측(Prometheus/Grafana)

## ⚠️ 미정 사항 (인프라 영향)

- **U6 시뮬 설정 API·배속 조작 범위** — 인프라 영향 없음
- **U7 파인튜닝 산출물 → GGUF 변환** — Colab 파인튜닝 산출물(HF 가중치/LoRA)은 Ollama가 못 먹습니다. `convert_hf_to_gguf.py` → `quantize` → `Modelfile` → `ollama create` 경로 필요. GGUF 보관처(S3 `models/`) 미정

> **종결됨**: U1(BE↔AI = 동기 REST) · U2(FE 채널 = WebSocket) · U3(LLM 서빙 = Ollama + Qwen GGUF) · U4(LLM 폭주 = `Semaphore(2)` + DB outbox) · U5(로그 = PG JSONB, Mongo 제거)
