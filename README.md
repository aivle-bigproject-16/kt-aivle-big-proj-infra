# 🔋 KT-AIVLE-big-proj-infra

> KT AIVLE 9기 빅프로젝트 / AI 06반 16조 — 배터리 셀 CT·RGB 결함검사 생산 시뮬레이션
> **인프라(배포·컨테이너·스토리지) 레포.** PartLeader: 김경순 / 서브: 공다연·김현민

CT(파우치)·RGB(원통) 독립 검사 파이프라인 2개 구조의 공통 데이터 레이어와 배포 뼈대를 소유합니다. 앱 서비스(FE/BE/AI/LLM)는 각 스택 레포에서 관리하며, 통합 배선은 후속 Phase에서 추가합니다.

## 📦 현재 범위 — 데이터 레이어

`docker compose up` 한 번으로 **PostgreSQL + MongoDB + MinIO**가 뜨고, MinIO 버킷 구조까지 자동 초기화됩니다.

| 서비스 | 이미지 | 역할 | 포트 |
| --- | --- | --- | --- |
| `postgres` | postgres:16-alpine | 메인 RDB (JSONB raw 포함) | 5432 |
| `mongo` | mongo:7 | 동작 로그 전용 | 27017 |
| `minio` | minio/minio | 개발 오브젝트 스토리지 (배포=AWS S3) | 9000(API) / 9001(콘솔) |
| `minio-init` | minio/mc | 버킷·프리픽스 1회 생성 후 종료 | — |

## 🚀 실행합니다

```bash
cp .env.example .env      # 값 확인 후 사용 (개발 기본값 그대로 가능)
docker compose up -d
docker compose ps         # postgres·mongo·minio = healthy 확인
docker compose logs minio-init   # 버킷 레이아웃 출력 확인
```

- MinIO 콘솔: http://localhost:9001 (`.env`의 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`)
- 종료: `docker compose down` / 볼륨까지 초기화: `docker compose down -v`

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
    endpoint_url=os.environ["S3_ENDPOINT"],      # 개발: http://localhost:9000 / 배포: 비움(AWS 기본)
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name=os.environ["S3_REGION"],
)
s3.list_objects_v2(Bucket=os.environ["S3_BUCKET"], Prefix="pool/ct/")
```

`S3_ENDPOINT`만 스위치하면 개발(MinIO)↔배포(AWS S3) 코드 동일 — 아키텍처 v2 §2 결정 11.

> 증강 파이프라인(공다연)은 이 버킷 위에서 `list/get/put/copy/delete_object`로 동작합니다. 개발은 AWS 키 발급 없이 MinIO 루트 자격으로 즉시 실습 가능. 배포 전 전용 서비스 계정(최소권한)으로 분리 예정.

## 🧭 다음 Phase (예정)

- 앱 서비스 배선: FE(nginx)·BE(spring)·AI(fastapi)·LLM(python) compose 편입 — submodule vs GHCR 이미지 방식 결정 후
- Dockerfile 템플릿(멀티스테이지) 배포
- EC2 단일 배포(프리티어 CPU, 데모 GPU=사전계산+리플레이 $0)
- (P2) CI/CD·EKS 부분 PoC·관측

## ⚠️ 미정 사항 (인프라 영향)

- **U1 BE↔AI 통신**: 동기 REST(baseline) vs Redis 큐(고도화) → Redis 컨테이너 편입 여부
- **U3 LLM 서빙**: Qwen API vs 로컬 vLLM → 로컬이면 GPU 상주 필요, 데모 $0 전략과 충돌
