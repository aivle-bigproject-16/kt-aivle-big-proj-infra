from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, status

from app.fixture import (
    FixtureError,
    FixtureMiss,
    ReplayCatalog,
    ReportCatalog,
    load_verified_json,
    request_fingerprint,
)
from app.models import (
    CellAnalysisAccepted,
    CellAnalysisCallback,
    CellAnalysisRequest,
    DailyReportRequest,
    IndividualReportRequest,
    ReportResponse,
)


logger = logging.getLogger("replay")
CallbackSender = Callable[
    [str, dict, str, float],
    Awaitable[None],
]


def _request_identity(request: CellAnalysisRequest) -> str:
    payload = request.model_dump(mode="json", by_alias=True)
    payload.pop("requestedAt", None)
    normalized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _positive_int(value: str, name: str, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return parsed


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be positive")
    return parsed


@dataclass(frozen=True)
class Settings:
    fixture_uri: str
    fixture_sha256: str
    report_fixture_uri: str
    report_fixture_sha256: str
    aws_region: str
    internal_api_key: str
    backend_callback_url: str
    delay_ms: int
    max_pending: int
    callback_timeout_seconds: float
    callback_max_attempts: int

    @classmethod
    def from_env(cls) -> "Settings":
        required = {
            "REPLAY_FIXTURE_URI": os.getenv("REPLAY_FIXTURE_URI", "").strip(),
            "REPLAY_FIXTURE_SHA256": os.getenv(
                "REPLAY_FIXTURE_SHA256", ""
            ).strip(),
            "AI_INTERNAL_API_KEY": os.getenv("AI_INTERNAL_API_KEY", "").strip(),
            "BACKEND_CALLBACK_URL": os.getenv(
                "BACKEND_CALLBACK_URL", ""
            ).strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                "missing required replay settings: " + ", ".join(missing)
            )
        report_uri = os.getenv("REPLAY_REPORT_FIXTURE_URI", "").strip()
        report_sha = os.getenv("REPLAY_REPORT_FIXTURE_SHA256", "").strip()
        if bool(report_uri) != bool(report_sha):
            raise RuntimeError(
                "report fixture URI and SHA-256 must be configured together"
            )
        return cls(
            fixture_uri=required["REPLAY_FIXTURE_URI"],
            fixture_sha256=required["REPLAY_FIXTURE_SHA256"],
            report_fixture_uri=report_uri,
            report_fixture_sha256=report_sha,
            aws_region=os.getenv("AWS_REGION", "ap-northeast-2").strip(),
            internal_api_key=required["AI_INTERNAL_API_KEY"],
            backend_callback_url=required["BACKEND_CALLBACK_URL"],
            delay_ms=_positive_int(
                os.getenv("REPLAY_DELAY_MS", "800"),
                "REPLAY_DELAY_MS",
                minimum=0,
            ),
            max_pending=_positive_int(
                os.getenv("REPLAY_MAX_PENDING", "64"),
                "REPLAY_MAX_PENDING",
            ),
            callback_timeout_seconds=_positive_float(
                os.getenv("CALLBACK_TIMEOUT_SECONDS", "10"),
                "CALLBACK_TIMEOUT_SECONDS",
            ),
            callback_max_attempts=_positive_int(
                os.getenv("CALLBACK_MAX_ATTEMPTS", "3"),
                "CALLBACK_MAX_ATTEMPTS",
            ),
        )


class RuntimeState:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.requests: dict[str, str] = {}
        self.tasks: set[asyncio.Task] = set()
        self.metrics = {
            "hits": 0,
            "misses": 0,
            "duplicates": 0,
            "conflicts": 0,
            "inFlight": 0,
            "callbackSuccess": 0,
            "callbackFailure": 0,
        }


async def _default_callback_sender(
    url: str,
    payload: dict,
    internal_api_key: str,
    timeout_seconds: float,
) -> None:
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"X-Internal-Api-Key": internal_api_key},
        )
        response.raise_for_status()


def create_app(
    settings: Settings | None = None,
    analysis_catalog: ReplayCatalog | None = None,
    report_catalog: ReportCatalog | None = None,
    callback_sender: CallbackSender | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime_settings = settings or Settings.from_env()
        catalog = analysis_catalog
        reports = report_catalog
        if catalog is None:
            payload, digest = load_verified_json(
                runtime_settings.fixture_uri,
                runtime_settings.fixture_sha256,
                runtime_settings.aws_region,
            )
            catalog = ReplayCatalog(payload, digest)
        if reports is None and runtime_settings.report_fixture_uri:
            payload, digest = load_verified_json(
                runtime_settings.report_fixture_uri,
                runtime_settings.report_fixture_sha256,
                runtime_settings.aws_region,
            )
            reports = ReportCatalog(payload, digest)

        application.state.settings = runtime_settings
        application.state.catalog = catalog
        application.state.reports = reports
        application.state.runtime = RuntimeState()
        application.state.callback_sender = (
            callback_sender or _default_callback_sender
        )
        logger.info(
            "replay fixture ready digest=%s entries=%d reports=%s",
            catalog.digest,
            len(catalog.entries),
            reports is not None,
        )
        yield
        tasks = list(application.state.runtime.tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    application = FastAPI(title="battery-replay", lifespan=lifespan)

    def current_settings() -> Settings:
        return application.state.settings

    def runtime() -> RuntimeState:
        return application.state.runtime

    async def deliver_callback(
        request: CellAnalysisRequest,
        recorded,
    ) -> None:
        state = runtime()
        configured = current_settings()
        try:
            if configured.delay_ms:
                await asyncio.sleep(configured.delay_ms / 1000)
            callback = application.state.catalog.build_callback(
                recorded,
                request,
                datetime.now(timezone.utc),
            )
            payload = callback.model_dump(mode="json", by_alias=True)
            last_error = None
            for attempt in range(1, configured.callback_max_attempts + 1):
                try:
                    await application.state.callback_sender(
                        request.callback_url,
                        payload,
                        configured.internal_api_key,
                        configured.callback_timeout_seconds,
                    )
                    state.metrics["callbackSuccess"] += 1
                    logger.info(
                        "callback delivered request=%s inspection=%s attempt=%d",
                        request.request_id,
                        request.inspection_id,
                        attempt,
                    )
                    return
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "callback failed request=%s inspection=%s attempt=%d",
                        request.request_id,
                        request.inspection_id,
                        attempt,
                    )
                    if attempt < configured.callback_max_attempts:
                        await asyncio.sleep(min(attempt, 3))
            state.metrics["callbackFailure"] += 1
            logger.error(
                "callback exhausted request=%s inspection=%s error=%s",
                request.request_id,
                request.inspection_id,
                type(last_error).__name__ if last_error else "unknown",
            )
        except Exception:
            state.metrics["callbackFailure"] += 1
            logger.exception(
                "callback construction failed request=%s inspection=%s",
                request.request_id,
                request.inspection_id,
            )
        finally:
            async with state.lock:
                state.metrics["inFlight"] -= 1

    def track(task: asyncio.Task) -> None:
        state = runtime()
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)

    @application.post(
        "/ai/cells/analyze",
        response_model=CellAnalysisAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def analyze_cell(
        request: CellAnalysisRequest,
        x_internal_api_key: str | None = Header(default=None),
    ) -> CellAnalysisAccepted:
        configured = current_settings()
        if (
            not x_internal_api_key
            or not secrets.compare_digest(
                x_internal_api_key, configured.internal_api_key
            )
        ):
            raise HTTPException(status_code=401, detail="unauthorized")
        if request.callback_url != configured.backend_callback_url:
            raise HTTPException(
                status_code=400,
                detail="callbackUrl does not match configured backend callback",
            )

        try:
            recorded = application.state.catalog.match(request)
            fingerprint = request_fingerprint(request.images)
        except FixtureMiss as exc:
            runtime().metrics["misses"] += 1
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        identity = _request_identity(request)
        state = runtime()
        async with state.lock:
            previous = state.requests.get(request.request_id)
            if previous is not None:
                if previous != identity:
                    state.metrics["conflicts"] += 1
                    raise HTTPException(
                        status_code=409,
                        detail="requestId was already used for another payload",
                    )
                state.metrics["duplicates"] += 1
            else:
                if state.metrics["inFlight"] >= configured.max_pending:
                    raise HTTPException(
                        status_code=503,
                        detail="replay callback queue is full",
                    )
                state.requests[request.request_id] = identity
                state.metrics["hits"] += 1
                state.metrics["inFlight"] += 1
                track(asyncio.create_task(deliver_callback(request, recorded)))

        logger.info(
            "replay accepted request=%s fingerprint=%s inspection=%s",
            request.request_id,
            fingerprint[:12],
            request.inspection_id,
        )
        return CellAnalysisAccepted(
            accepted=True,
            request_id=request.request_id,
            inspection_id=request.inspection_id,
            battery_cell_id=request.battery_cell_id,
            status="ACCEPTED",
            accepted_at=datetime.now(timezone.utc),
        )

    @application.post(
        "/vlm/reports/individual",
        response_model=ReportResponse,
    )
    async def individual_report(
        request: IndividualReportRequest,
    ) -> ReportResponse:
        reports = application.state.reports
        if reports is None:
            raise HTTPException(
                status_code=503,
                detail="report replay fixture is not configured",
            )
        try:
            return reports.individual_response(
                request.cellSerialNo,
                request.inspectionId,
                request.sourceInspectionIds,
            )
        except FixtureMiss as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post(
        "/vlm/reports/daily",
        response_model=ReportResponse,
    )
    async def daily_report(request: DailyReportRequest) -> ReportResponse:
        reports = application.state.reports
        if reports is None:
            raise HTTPException(
                status_code=503,
                detail="report replay fixture is not configured",
            )
        return reports.daily_response(request.daily_data)

    @application.get("/health")
    async def health() -> dict:
        catalog = application.state.catalog
        reports = application.state.reports
        return {
            "status": "ok",
            "mode": "REPLAY",
            "fixture": {
                "sha256": catalog.digest,
                "inspectionCount": len(catalog.entries),
            },
            "reports": {
                "enabled": reports is not None,
                "sha256": reports.digest if reports else None,
                "individualCount": len(reports.individuals) if reports else 0,
                "scope": (
                    "individual catalog; daily deterministic summary"
                    if reports
                    else None
                ),
            },
            "metrics": dict(runtime().metrics),
        }

    return application


app = create_app()
