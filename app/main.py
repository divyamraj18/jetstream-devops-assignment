import os
from datetime import datetime, timezone
from typing import List

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

from models import ArticleCreate, ArticleResponse, ArticleUpdate

app = FastAPI(
    title="Jetstream Articles API",
    description="CRUD API for articles, backed by a MongoDB replica set.",
    version="1.0.0",
)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB_NAME", "jetstream")

client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]
articles_collection = db["articles"]


def article_to_response(doc: dict) -> ArticleResponse:
    return ArticleResponse(
        id=str(doc["_id"]),
        title=doc["title"],
        content=doc["content"],
        author=doc["author"],
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def parse_object_id(article_id: str) -> ObjectId:
    try:
        return ObjectId(article_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid article id")


@app.get("/healthz", status_code=status.HTTP_200_OK)
async def liveness():
    """Liveness probe: only confirms the process is up and serving requests."""
    return {"status": "alive"}


@app.get("/ready", status_code=status.HTTP_200_OK)
async def readiness():
    """Readiness probe: confirms the MongoDB connection is actually usable."""
    try:
        await db.command("ping")
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB not reachable")
    return {"status": "ready"}


@app.post("/articles", response_model=ArticleResponse, status_code=status.HTTP_201_CREATED)
async def create_article(article: ArticleCreate):
    now = datetime.now(timezone.utc)
    doc = article.model_dump()
    doc["created_at"] = now
    doc["updated_at"] = now
    result = await articles_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return article_to_response(doc)


@app.get("/articles", response_model=List[ArticleResponse])
async def list_articles():
    docs = await articles_collection.find().to_list(length=None)
    return [article_to_response(doc) for doc in docs]


@app.get("/articles/{article_id}", response_model=ArticleResponse)
async def get_article(article_id: str):
    doc = await articles_collection.find_one({"_id": parse_object_id(article_id)})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found")
    return article_to_response(doc)


@app.put("/articles/{article_id}", response_model=ArticleResponse)
async def update_article(article_id: str, article: ArticleUpdate):
    updates = {k: v for k, v in article.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")
    updates["updated_at"] = datetime.now(timezone.utc)

    doc = await articles_collection.find_one_and_update(
        {"_id": parse_object_id(article_id)},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found")
    return article_to_response(doc)


@app.delete("/articles/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_article(article_id: str):
    result = await articles_collection.delete_one({"_id": parse_object_id(article_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found")
