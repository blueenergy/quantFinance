"""Read/delete portfolio research artifacts from S3 (Compose API serving K8s jobs)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ArtifactStorageUnavailable(RuntimeError):
    """S3 is configured for an artifact but cannot currently be accessed."""


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def s3_env_complete() -> bool:
    return bool(
        _env("PORTFOLIO_RESEARCH_S3_ENDPOINT")
        and _env("PORTFOLIO_RESEARCH_S3_BUCKET")
        and _env("PORTFOLIO_RESEARCH_S3_ACCESS_KEY")
        and _env("PORTFOLIO_RESEARCH_S3_SECRET_KEY")
    )


def artifact_uses_s3(artifact: Optional[Dict[str, Any]]) -> bool:
    if not artifact:
        return False
    if (artifact.get("storage") or "").lower() == "s3":
        return True
    return bool(artifact.get("s3"))


def _s3_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=_env("PORTFOLIO_RESEARCH_S3_ENDPOINT"),
        aws_access_key_id=_env("PORTFOLIO_RESEARCH_S3_ACCESS_KEY"),
        aws_secret_access_key=_env("PORTFOLIO_RESEARCH_S3_SECRET_KEY"),
        region_name=_env("PORTFOLIO_RESEARCH_S3_REGION") or "us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )


def _s3_meta(artifact: Dict[str, Any]) -> Dict[str, Any]:
    return dict(artifact.get("s3") or {})


def artifact_prefix(job_id: str, result_id: str) -> str:
    return f"portfolio-research/{job_id}/{result_id[:8]}/"


def _artifact_bucket(artifact: Dict[str, Any]) -> str:
    configured_bucket = _env("PORTFOLIO_RESEARCH_S3_BUCKET")
    artifact_bucket = _s3_meta(artifact).get("bucket")
    if artifact_bucket and artifact_bucket != configured_bucket:
        raise ArtifactStorageUnavailable("artifact bucket does not match configured S3 bucket")
    return configured_bucket


def get_object_bytes(artifact: Dict[str, Any], key: str) -> Optional[bytes]:
    if not s3_env_complete():
        raise ArtifactStorageUnavailable("S3 configuration is incomplete")
    bucket = _artifact_bucket(artifact)
    if not bucket or not key:
        return None
    try:
        client = _s3_client()
        response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except Exception as exc:
        response = getattr(exc, "response", {}) or {}
        code = str((response.get("Error") or {}).get("Code") or "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        logger.warning("S3 get_object failed bucket=%s key=%s: %s", bucket, key, exc)
        raise ArtifactStorageUnavailable(f"S3 get_object failed: {code or type(exc).__name__}") from exc


def combo_object_key(artifact: Dict[str, Any], combo_key: str, file_rel: Optional[str]) -> Optional[str]:
    meta = _s3_meta(artifact)
    combos_prefix = meta.get("combos_prefix")
    if not combos_prefix:
        return None
    if file_rel:
        name = Path(file_rel).name
    else:
        name = f"{combo_key}.json"
    if combos_prefix.endswith("/"):
        return f"{combos_prefix}{name}"
    return f"{combos_prefix}/{name}"


def delete_artifact_prefix(artifact: Dict[str, Any], *, expected_prefix: str) -> Dict[str, Any]:
    """Best-effort delete of all objects under artifact ``s3.prefix``."""
    stats: Dict[str, Any] = {"removed": [], "errors": []}
    if not artifact_uses_s3(artifact):
        return stats
    if not s3_env_complete():
        stats["errors"].append("S3 configuration is incomplete")
        return stats
    meta = _s3_meta(artifact)
    prefix = meta.get("prefix")
    if prefix != expected_prefix:
        stats["errors"].append(f"refused unsafe S3 prefix: {prefix!r}")
        return stats
    try:
        bucket = _artifact_bucket(artifact)
        client = _s3_client()
        keys: List[str] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                keys.append(obj["Key"])
        for offset in range(0, len(keys), 1000):
            batch = keys[offset : offset + 1000]
            client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in batch]})
            stats["removed"].extend(batch)
    except Exception as exc:
        stats["errors"].append(str(exc))
        logger.warning("S3 delete prefix failed: %s", exc)
    return stats


def fetch_report_json_bytes(artifact: Dict[str, Any]) -> Optional[bytes]:
    meta = _s3_meta(artifact)
    key = meta.get("json_key")
    if not key:
        return None
    return get_object_bytes(artifact, key)


def fetch_report_html_bytes(artifact: Dict[str, Any]) -> Optional[bytes]:
    meta = _s3_meta(artifact)
    key = meta.get("html_key")
    if not key:
        return None
    return get_object_bytes(artifact, key)


def fetch_combo_bytes(artifact: Dict[str, Any], combo_key: str, file_rel: Optional[str]) -> Optional[bytes]:
    key = combo_object_key(artifact, combo_key, file_rel)
    if not key:
        return None
    return get_object_bytes(artifact, key)
