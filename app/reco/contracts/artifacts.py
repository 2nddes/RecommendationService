from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from app.reco.contracts.features import FEATURE_SCHEMA_VERSION, MODEL_META_VERSION, model_feature_names


@dataclass(frozen=True)
class ArtifactManifest:
    component: str
    model_name: str
    artifact_path: str
    created_at: str
    feature_schema_version: str
    model_meta_version: str
    feature_names: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def build_manifest(*, component: str, model_name: str, artifact_path: str, details: dict[str, Any] | None = None) -> ArtifactManifest:
    model_key = f"{component}.{model_name}"
    return ArtifactManifest(
        component=component,
        model_name=model_name,
        artifact_path=artifact_path,
        created_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        model_meta_version=MODEL_META_VERSION,
        feature_names=model_feature_names(model_key),
        details=dict(details or {}),
    )


def write_manifest(*, component: str, model_name: str, artifact_path: str, details: dict[str, Any] | None = None) -> str:
    manifest = build_manifest(
        component=component,
        model_name=model_name,
        artifact_path=artifact_path,
        details=details,
    )
    manifest_path = Path(f"{artifact_path}.manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(asdict(manifest), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(manifest_path)
