from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Callable

from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response

from api.db.database import SessionLocal
from api.infra.region_pool import DEFAULT_REGION_ID


LOGGER = logging.getLogger("memoryos.versioning")
MAX_SUPPORTED_API_VERSION = 1


@dataclass(frozen=True, slots=True)
class DeprecatedVersionInfo:
    sunset_at: datetime
    migration_guide_url: str
    successor_version: str


@dataclass(frozen=True, slots=True)
class DeprecatedFieldNotice:
    field_path: str
    header_field_name: str
    sunset_at: datetime
    migration_guide_url: str
    replacement_field: str | None = None


DEPRECATED_API_VERSIONS: dict[int, DeprecatedVersionInfo] = {}


def register_deprecated_field(
    request: Request,
    *,
    field_path: str,
    header_field_name: str,
    sunset_at: datetime,
    migration_guide_url: str,
    replacement_field: str | None = None,
) -> None:
    notices = getattr(request.state, "deprecated_fields", None)
    if not isinstance(notices, list):
        notices = []
        request.state.deprecated_fields = notices
    notices.append(
        DeprecatedFieldNotice(
            field_path=field_path,
            header_field_name=header_field_name,
            sunset_at=sunset_at,
            migration_guide_url=migration_guide_url,
            replacement_field=replacement_field,
        )
    )


class VersioningMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        api_version = self._parse_api_version(request.url.path)
        if api_version is None:
            return await call_next(request)

        if api_version > MAX_SUPPORTED_API_VERSION:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "unsupported_api_version",
                    "max_supported": f"v{MAX_SUPPORTED_API_VERSION}",
                },
            )

        request.state.api_version = api_version
        response = await call_next(request)

        deprecated_version = DEPRECATED_API_VERSIONS.get(api_version)
        if deprecated_version is not None:
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = deprecated_version.sunset_at.isoformat()
            response.headers["Link"] = (
                f"<{deprecated_version.migration_guide_url}>; rel='successor-version'"
            )

        deprecated_fields = getattr(request.state, "deprecated_fields", None)
        if isinstance(deprecated_fields, list) and deprecated_fields:
            earliest_sunset = min(notice.sunset_at for notice in deprecated_fields)
            migration_guide_url = deprecated_fields[0].migration_guide_url
            response.headers.setdefault("Deprecation", "true")
            response.headers.setdefault("Sunset", earliest_sunset.isoformat())
            response.headers.setdefault(
                "Link",
                f"<{migration_guide_url}>; rel='successor-version'",
            )
            response.headers["X-MemoryOS-Deprecated-Fields"] = "; ".join(
                f"{notice.header_field_name} (sunset: {notice.sunset_at.date().isoformat()}); "
                f"see {notice.migration_guide_url}"
                for notice in deprecated_fields
            )
            await self._record_deprecated_usage(request, deprecated_fields)

        return response

    @staticmethod
    def _parse_api_version(path: str) -> int | None:
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            return None
        version_segment = segments[0]
        if not version_segment.startswith("v"):
            return None
        try:
            return int(version_segment[1:])
        except ValueError:
            return None

    async def _record_deprecated_usage(
        self,
        request: Request,
        deprecated_fields: list[DeprecatedFieldNotice],
    ) -> None:
        tenant_id = getattr(request.state, "tenant_id", None)
        if not tenant_id:
            return

        for notice in deprecated_fields:
            LOGGER.info(
                json.dumps(
                    {
                        "event": "deprecated_field_used",
                        "tenant_id": str(tenant_id),
                        "deprecated_field": notice.field_path,
                        "api_version": f"v{getattr(request.state, 'api_version', MAX_SUPPORTED_API_VERSION)}",
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )
            )

        region_id = getattr(request.state, "region_id", None) or DEFAULT_REGION_ID
        region_pool = getattr(request.app.state, "region_pool", None)
        session_context = region_pool.get_db(region_id) if region_pool is not None else SessionLocal()
        try:
            async with session_context as session:
                for notice in deprecated_fields:
                    await session.execute(
                        text(
                            """
                            INSERT INTO tenant_deprecation_usage (
                                id,
                                tenant_id,
                                api_version,
                                field_path,
                                first_used_at,
                                last_used_at,
                                usage_count
                            ) VALUES (
                                CAST(:id AS uuid),
                                CAST(:tenant_id AS uuid),
                                :api_version,
                                :field_path,
                                NOW(),
                                NOW(),
                                1
                            )
                            ON CONFLICT (tenant_id, api_version, field_path)
                            DO UPDATE SET
                                last_used_at = NOW(),
                                usage_count = tenant_deprecation_usage.usage_count + 1
                            """
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "tenant_id": str(tenant_id),
                            "api_version": f"v{getattr(request.state, 'api_version', MAX_SUPPORTED_API_VERSION)}",
                            "field_path": notice.field_path,
                        },
                    )
                await session.commit()
        except Exception as exc:
            LOGGER.warning(
                "deprecated_field_usage_persist_failed tenant_id=%s error=%s",
                tenant_id,
                type(exc).__name__,
            )
