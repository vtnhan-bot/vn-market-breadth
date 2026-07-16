"""Cloudflare R2 publish leg for the VN market-breadth dashboard.

This is the Cloudflare R2 side of a DUAL publish: GCS (gs://vn-market-breadth)
remains the primary, authoritative destination until a deliberate cutover
decision is made. Everything in this module is best-effort and self-disables
when R2 credentials are absent, so it is safe to deploy and call from
production before the Cloudflare account even exists — it is a no-op until
activated. No public function ever raises; failures are logged and reported
via a boolean return, and the pipeline's GCS publish path is never affected.

Activate by setting all three of:
    R2_ACCOUNT_ID
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
R2_BUCKET is optional (defaults to "vn-market-breadth").
"""
from __future__ import annotations

import functools
import logging
import os
import sys
from pathlib import Path

LOGGER = logging.getLogger("r2_publish")

_BUCKET_DEFAULT = "vn-market-breadth"
_CACHE_CONTROL = "no-cache, must-revalidate"
_REQUIRED_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")

_warned = False


def _missing_vars() -> list[str]:
    return [name for name in _REQUIRED_VARS if not os.environ.get(name)]


def is_configured() -> bool:
    return not _missing_vars()


def _bucket() -> str:
    return os.environ.get("R2_BUCKET") or _BUCKET_DEFAULT


def _warn_disabled_once(reason: str) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    LOGGER.info("R2 publish disabled (%s) - GCS remains the sole destination", reason)


@functools.lru_cache(maxsize=1)
def _get_client():
    """Build (and cache) the boto3 S3-compatible client for R2.

    boto3 is imported here rather than at module scope so a missing package
    cannot break anything that merely imports this module. Only called once
    all three required env vars are confirmed present.
    """
    try:
        import boto3
    except ImportError:
        _warn_disabled_once("boto3 not installed")
        return None
    try:
        return boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
    except Exception as exc:
        LOGGER.warning("R2 client init failed (non-fatal): %s", exc)
        return None


def put_bytes(key: str, data: bytes | str, content_type: str) -> bool:
    """Upload `data` to R2 under `key`. Best-effort: never raises."""
    try:
        missing = _missing_vars()
        if missing:
            _warn_disabled_once(f"missing {', '.join(missing)}")
            return False
        client = _get_client()
        if client is None:
            return False
        body = data.encode("utf-8") if isinstance(data, str) else data
        client.put_object(
            Bucket=_bucket(),
            Key=key,
            Body=body,
            ContentType=content_type,
            CacheControl=_CACHE_CONTROL,
        )
        LOGGER.info("R2 published %s (%d bytes)", key, len(body))
        return True
    except Exception as exc:
        LOGGER.warning("R2 publish FAILED for %s (non-fatal): %s", key, exc)
        return False


def put_file(key: str, path: str | Path, content_type: str) -> bool:
    """Upload the file at `path` to R2 under `key`. Best-effort: never raises."""
    try:
        data = Path(path).read_bytes()
    except Exception as exc:
        LOGGER.warning("R2 publish FAILED to read %s for %s (non-fatal): %s", path, key, exc)
        return False
    return put_bytes(key, data, content_type)


def _main(argv: list[str]) -> int:
    if len(argv) != 4:
        LOGGER.error("usage: r2_publish.py <local_path> <key> <content_type>")
        return 1
    _, local_path, key, content_type = argv
    if not is_configured():
        LOGGER.info("R2 publish disabled - skipping %s (exit 0, pre-cutover no-op)", key)
        return 0
    return 0 if put_file(key, local_path, content_type) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s - %(message)s", datefmt="%H:%M:%S")
    raise SystemExit(_main(sys.argv))
