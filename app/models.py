"""Pydantic models for API requests and responses."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class GeneratedImage(BaseModel):
    url: str
    mime_type: str


class GeneratedAssets(BaseModel):
    with_logo: GeneratedImage
    with_placeholder: GeneratedImage


class CanvaInfo(BaseModel):
    configured: bool
    import_url: Optional[str] = None


class AssetsResult(BaseModel):
    type: Literal["assets"] = "assets"
    brand: Optional[str] = None
    creative_type: Optional[str] = None
    purpose: Optional[str] = None
    master_prompt: str
    brand_profile: Optional[str] = None
    assets: GeneratedAssets
    canva: CanvaInfo


class IntakeResult(BaseModel):
    type: Literal["intake"] = "intake"
    text: str
    missing_fields: list[str] = Field(default_factory=list)
    suggestions: dict[str, Any] = Field(default_factory=dict)


class MessageResult(BaseModel):
    type: Literal["message"] = "message"
    text: str


class Brand(BaseModel):
    id: str
    brand_name: str
    brand_metadata: dict[str, Any] = Field(default_factory=dict)


class Creative(BaseModel):
    id: str
    brand_id: str
    file_name: str
    file_type: str
    file_url: str
    creative_metadata: dict[str, Any] = Field(default_factory=dict)


class BrandDetail(BaseModel):
    brand: Brand
    creatives: list[Creative]
