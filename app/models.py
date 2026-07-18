from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


class ArticleBase(BaseModel):
    title: str = Field(..., example="Zero-SPOF Architecture on Kubernetes")
    content: str = Field(..., example="This article explains how to design a highly available stack.")
    author: str = Field(..., example="Divyam Raj")


class ArticleCreate(ArticleBase):
    pass


class ArticleUpdate(BaseModel):
    title: Optional[str] = Field(None, example="Updated Title")
    content: Optional[str] = Field(None, example="Updated content.")
    author: Optional[str] = Field(None, example="Divyam Raj")


class ArticleResponse(ArticleBase):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "65f1c2e4a1b2c3d4e5f6a7b8",
                "title": "Zero-SPOF Architecture on Kubernetes",
                "content": "This article explains how to design a highly available stack.",
                "author": "Divyam Raj",
                "created_at": "2026-07-18T10:00:00Z",
                "updated_at": "2026-07-18T10:00:00Z",
            }
        }
    )

    id: str = Field(..., example="65f1c2e4a1b2c3d4e5f6a7b8")
    created_at: datetime
    updated_at: datetime
