"""共享文件用途和货物领域目录；不含租户数据或供应商配置。"""
from fastapi import APIRouter
from pydantic import BaseModel
from app.api.dependencies import CurrentUser
from app.documents.type_catalog import catalog_view

router = APIRouter(tags=["documents"])


class DocumentTypeEntry(BaseModel):
    code: str
    label: str
    group: str
    base_type: str
    layout: str
    review_focus: list[str]
    profile_version: str
    capability_state: str
    verified_sample_ids: list[str]
    titles_by_language: dict[str, list[str]]


class CargoProfileEntry(BaseModel):
    code: str
    label: str
    review_focus: str


class DocumentCatalogView(BaseModel):
    version: str
    items: list[DocumentTypeEntry]
    cargo_profiles: list[CargoProfileEntry]


@router.get("/document-catalog", response_model=DocumentCatalogView)
async def get_document_catalog(principal: CurrentUser):
    return catalog_view()
