from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class BackendContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class CellImageRequest(BackendContractModel):
    image_id: int
    image_type: Literal["CT", "RGB"]
    bucket_name: str = Field(min_length=1)
    object_key: str = Field(min_length=1)


class CellAnalysisRequest(BackendContractModel):
    request_id: str = Field(min_length=1)
    batch_id: int
    inspection_id: int
    battery_cell_id: int
    cell_serial_no: str = Field(min_length=1)
    requested_at: datetime
    callback_url: str = Field(min_length=1)
    images: list[CellImageRequest] = Field(min_length=1)


class CellAnalysisAccepted(BackendContractModel):
    accepted: bool
    request_id: str
    inspection_id: int
    battery_cell_id: int
    status: Literal["ACCEPTED"]
    accepted_at: datetime


class CallbackBoundingBox(BackendContractModel):
    x: int
    y: int
    width: int = Field(ge=0)
    height: int = Field(ge=0)


class CallbackDefect(BackendContractModel):
    defect_type: Literal["SWELLING", "SPOT", "MICRO_DEFECT", "CRACK"]
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: CallbackBoundingBox


class ImageAnalysisResult(BackendContractModel):
    image_id: int
    image_type: Literal["CT", "RGB"]
    label: Literal["PASS", "REJECT", "FAIL"]
    confidence: float = Field(ge=0.0, le=1.0)
    defects: list[CallbackDefect]
    raw_response: dict[str, Any] | None
    latency_ms: int = Field(ge=0)
    error_code: str | None = None
    error_message: str | None = None


class CellAnalysisCallback(BackendContractModel):
    request_id: str
    batch_id: int
    inspection_id: int
    battery_cell_id: int
    cell_serial_no: str
    cell_status: Literal["COMPLETED", "FAILED"]
    final_label: Literal["PASS", "REJECT"] | None
    failure_type: Literal["CAPTURE", "AI"] | None
    failure_reason: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    completed_at: datetime
    image_results: list[ImageAnalysisResult]

    @model_validator(mode="after")
    def validate_status_contract(self):
        if self.cell_status == "FAILED":
            if self.final_label is not None:
                raise ValueError("FAILED callback requires final_label=None")
            if self.failure_type is None or not self.failure_reason:
                raise ValueError("FAILED callback requires failure information")
        elif self.final_label not in {"PASS", "REJECT"}:
            raise ValueError("COMPLETED callback requires PASS or REJECT")
        return self


class IndividualReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cellSerialNo: str
    inspectionId: int | None
    totalImages: int
    cellSize: list[float] | None
    pointGroups: list[list[float]]
    ctVoidRatio: float | None
    rgbDefectRate: float | None
    defectInfo: list[dict[str, Any]]


class DailyDefectCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defectType: str = Field(min_length=1)
    count: int = Field(ge=0)


class DailySummaryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    totalCount: int = Field(ge=0)
    passCount: int = Field(ge=0)
    rejectCount: int = Field(ge=0)
    failedCount: int = Field(ge=0)
    prevTotalCount: int = Field(ge=0)
    prevRejectCount: int = Field(ge=0)
    defects: list[DailyDefectCount]


class DailyReportData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reportDate: str = Field(min_length=1)
    summaryData: DailySummaryData


class DailyReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_data: DailyReportData


class ReportResponse(BaseModel):
    status: str
    title: str | None
    content: str | None
    failureReason: str | None
