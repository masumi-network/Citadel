from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import inspect
import os
import re
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, Callable, Iterator, Literal, Sequence
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.dataset_database_handler import (
    DatasetDatabaseHandlerInterface,
)
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector import get_vectordb_config
from cognee.infrastructure.databases.vector.create_vector_engine import create_vector_engine
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.modules.users.models import DatasetDatabase, User
from pydantic import Field
from qdrant_client import AsyncQdrantClient, models

QDRANT_CLIENT_VERSION = "1.19.0"
_SCOPE_DATASET_FIELD = "citadel_dataset_scope"
_GENERATION_FIELD = "citadel_generation_id"
_LOGICAL_COLLECTION_FIELD = "citadel_logical_collection"


class QdrantScopeError(RuntimeError):
    pass


class QdrantConfigurationError(RuntimeError):
    pass


class QdrantProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class QdrantOperationScope:
    mode: Literal["read", "write", "admin"]
    generation_id: str
    dataset: str


_QDRANT_SCOPE: contextvars.ContextVar[QdrantOperationScope | None] = contextvars.ContextVar(
    "citadel_qdrant_scope", default=None
)


def _required_text(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise QdrantScopeError(f"Qdrant {field_name} must not be empty")
    return normalized


@contextlib.contextmanager
def qdrant_scope(
    *,
    mode: Literal["read", "write", "admin"],
    generation_id: str,
    dataset: str,
) -> Iterator[QdrantOperationScope]:
    if mode not in {"read", "write", "admin"}:
        raise QdrantScopeError(f"unsupported Qdrant scope mode: {mode}")
    requested = QdrantOperationScope(
        mode=mode,
        generation_id=_required_text(generation_id, "generation scope"),
        dataset=_required_text(dataset, "dataset scope"),
    )
    current = _QDRANT_SCOPE.get()
    if current is not None and current != requested:
        raise QdrantScopeError(
            "Qdrant scope conflict: nested operation changed mode, generation, or dataset"
        )
    token = _QDRANT_SCOPE.set(requested)
    try:
        yield requested
    finally:
        _QDRANT_SCOPE.reset(token)


def _hash(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _logical_slug(logical_collection: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", logical_collection).strip("_")
    return (slug or "collection")[:48]


def physical_collection_name(
    generation_id: str,
    dataset: str | None,
    logical_collection: str,
) -> str:
    generation = _required_text(generation_id, "generation scope")
    logical = _required_text(logical_collection, "logical collection")
    generation_hash = _hash(generation)
    logical_suffix = f"{_logical_slug(logical)}_{_hash(logical, 10)}"
    if dataset is not None:
        _required_text(dataset, "dataset scope")
    return f"citadel_g_{generation_hash}_{logical_suffix}"


def stored_point_id(generation_id: str, dataset: str, raw_id: str | UUID) -> UUID:
    generation = _required_text(generation_id, "generation scope")
    dataset_name = _required_text(dataset, "dataset scope")
    point_id = _required_text(str(raw_id), "point id")
    return uuid5(NAMESPACE_URL, f"citadel:{generation}:{dataset_name}:{point_id}")


def _serialize(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(item) for item in value]
    if hasattr(value, "model_dump"):
        return _serialize(value.model_dump())
    return value


def _node_set_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item.strip()
        else:
            name = str(getattr(item, "name", "")).strip()
        if name and name not in names:
            names.append(name)
    return names


class IndexSchema(DataPoint):
    text: str
    document_id: str | None = None
    document_name: str | None = None
    chunk_index: int | None = None
    source_chunk_id: str | None = None
    importance_weight: float | None = 0.5
    source_pipeline: str | None = None
    source_task: str | None = None
    source_node_set: str | None = None
    source_user: str | None = None
    source_content_hash: str | None = None
    metadata: dict = Field(default_factory=lambda: {"index_fields": ["text"]})
    belongs_to_set: list[str] = Field(default_factory=list)


class CitadelQdrantDatasetDatabaseHandler(DatasetDatabaseHandlerInterface):
    @classmethod
    async def create_dataset(cls, dataset_id: UUID | None, user: User | None) -> dict[str, Any]:
        del user
        if dataset_id is None:
            raise QdrantConfigurationError("Qdrant dataset creation requires a dataset ID")
        vector_config = get_vectordb_config()
        if vector_config.vector_db_provider != "qdrant":
            raise QdrantConfigurationError(
                "Citadel Qdrant dataset handler requires VECTOR_DB_PROVIDER=qdrant"
            )
        return {
            "vector_database_provider": "qdrant",
            "vector_database_url": vector_config.vector_db_url,
            "vector_database_key": vector_config.vector_db_key,
            "vector_database_name": str(dataset_id),
            "vector_dataset_database_handler": "qdrant",
        }

    @classmethod
    async def delete_dataset(cls, dataset_database: DatasetDatabase) -> None:
        vector_engine = create_vector_engine(
            vector_db_provider=dataset_database.vector_database_provider,
            vector_db_url=dataset_database.vector_database_url,
            vector_db_key=dataset_database.vector_database_key,
            vector_db_name=dataset_database.vector_database_name,
        )
        await vector_engine.prune()


class CitadelQdrantAdapter(VectorDBInterface):
    name = "CitadelQdrant"

    def __init__(
        self,
        url: str,
        api_key: str,
        embedding_engine: EmbeddingEngine,
        database_name: str = "cognee_db",
        timeout: int = 120,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        parsed = urlparse(str(url).strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise QdrantConfigurationError(
                "VECTOR_DB_URL must be an HTTP(S) Qdrant origin without credentials, query, or fragment"
            )
        if not str(api_key).strip():
            raise QdrantConfigurationError("VECTOR_DB_KEY must not be empty")
        self.url = str(url).strip().rstrip("/")
        self.api_key = str(api_key).strip()
        self.embedding_engine = embedding_engine
        configured_generation = os.getenv("CITADEL_GENERATION_ID", "").strip()
        self.bound_dataset = (
            _required_text(database_name, "dataset scope")
            if configured_generation
            else None
        )
        self.generation_id = _required_text(
            configured_generation or database_name,
            "generation scope",
        )
        self.timeout = int(timeout)
        self._client_factory = client_factory
        self._collection_locks: dict[str, asyncio.Lock] = {}

    def get_qdrant_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        return AsyncQdrantClient(
            url=self.url,
            api_key=self.api_key,
            timeout=self.timeout,
        )

    async def _close_client(self, client: Any) -> None:
        result = client.close()
        if inspect.isawaitable(result):
            await result

    def _scope(self, required_modes: set[str]) -> QdrantOperationScope:
        scope = _QDRANT_SCOPE.get()
        if scope is None:
            if self.bound_dataset is None:
                raise QdrantScopeError("Qdrant operation requires an explicit Citadel scope")
            mode = next(
                candidate
                for candidate in ("read", "write", "admin")
                if candidate in required_modes
            )
            return QdrantOperationScope(
                mode=mode,
                generation_id=self.generation_id,
                dataset=self.bound_dataset,
            )
        if scope.mode not in required_modes:
            expected = ", ".join(sorted(required_modes))
            raise QdrantScopeError(
                f"Qdrant scope mode {scope.mode!r} cannot perform operation requiring {expected}"
            )
        if scope.generation_id != self.generation_id:
            raise QdrantScopeError(
                "Qdrant scope conflict: operation generation does not match adapter generation"
            )
        if self.bound_dataset is not None and scope.dataset != self.bound_dataset:
            raise QdrantScopeError(
                "Qdrant scope conflict: operation dataset does not match adapter dataset"
            )
        return scope

    def current_dataset(self, *, required_mode: str) -> str:
        return self._scope({required_mode}).dataset

    def scoped_collection(self, logical_collection: str, *, required_mode: str) -> str:
        scope = self._scope({required_mode})
        return physical_collection_name(
            self.generation_id,
            scope.dataset,
            logical_collection,
        )

    def internal_collection(self, logical_collection: str) -> str:
        return physical_collection_name(self.generation_id, None, logical_collection)

    def stored_id(self, raw_id: str | UUID, *, dataset: str) -> UUID:
        return stored_point_id(self.generation_id, dataset, raw_id)

    def _collection_lock(self, collection_name: str) -> asyncio.Lock:
        lock = self._collection_locks.get(collection_name)
        if lock is None:
            lock = asyncio.Lock()
            self._collection_locks[collection_name] = lock
        return lock

    def _scope_filter(
        self,
        dataset: str,
        *,
        node_names: Sequence[str] | None = None,
        node_name_filter_operator: str = "OR",
        point_ids: Sequence[str | UUID] | None = None,
    ) -> models.Filter:
        conditions: list[Any] = [
            models.FieldCondition(
                key=_GENERATION_FIELD,
                match=models.MatchValue(value=self.generation_id),
            ),
            models.FieldCondition(
                key=_SCOPE_DATASET_FIELD,
                match=models.MatchValue(value=dataset),
            ),
        ]
        if point_ids:
            conditions.append(models.HasIdCondition(has_id=list(point_ids)))
        if node_names:
            names = [str(name) for name in node_names if str(name).strip()]
            if node_name_filter_operator == "AND":
                conditions.extend(
                    models.FieldCondition(
                        key="belongs_to_set",
                        match=models.MatchAny(any=[name]),
                    )
                    for name in names
                )
            elif names:
                conditions.append(
                    models.FieldCondition(
                        key="belongs_to_set",
                        match=models.MatchAny(any=names),
                    )
                )
        return models.Filter(must=conditions)

    async def _collection_exists(self, collection_name: str) -> bool:
        client = self.get_qdrant_client()
        try:
            return bool(await client.collection_exists(collection_name))
        except Exception as exc:
            raise QdrantProviderError("Qdrant collection check failed") from exc
        finally:
            await self._close_client(client)

    async def _ensure_collection(self, collection_name: str) -> None:
        async with self._collection_lock(collection_name):
            client = self.get_qdrant_client()
            try:
                if await client.collection_exists(collection_name):
                    return
                try:
                    await client.create_collection(
                        collection_name=collection_name,
                        vectors_config={
                            "text": models.VectorParams(
                                size=self.embedding_engine.get_vector_size(),
                                distance=models.Distance.COSINE,
                            )
                        },
                        hnsw_config=models.HnswConfigDiff(
                            m=16,
                            ef_construct=100,
                            payload_m=16,
                        ),
                    )
                except Exception:
                    if not await client.collection_exists(collection_name):
                        raise
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=_GENERATION_FIELD,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=_SCOPE_DATASET_FIELD,
                    field_schema=models.KeywordIndexParams(
                        type=models.KeywordIndexType.KEYWORD,
                        is_tenant=True,
                    ),
                    wait=True,
                )
            except Exception as exc:
                raise QdrantProviderError("Qdrant collection creation failed") from exc
            finally:
                await self._close_client(client)

    async def has_collection(self, collection_name: str) -> bool:
        scope = self._scope({"read", "write", "admin"})
        physical = physical_collection_name(
            self.generation_id,
            scope.dataset,
            collection_name,
        )
        return await self._collection_exists(physical)

    async def create_collection(self, collection_name: str, payload_schema: Any = None) -> None:
        del payload_schema
        scope = self._scope({"write"})
        await self._ensure_collection(
            physical_collection_name(self.generation_id, scope.dataset, collection_name)
        )

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)

    def _payload(
        self,
        data_point: DataPoint,
        *,
        logical_collection: str,
        dataset: str,
    ) -> dict[str, Any]:
        payload = _serialize(data_point.model_dump())
        payload["belongs_to_set"] = _node_set_names(payload.get("belongs_to_set"))
        payload[_GENERATION_FIELD] = self.generation_id
        payload[_SCOPE_DATASET_FIELD] = dataset
        payload[_LOGICAL_COLLECTION_FIELD] = logical_collection
        payload["citadel_original_id"] = str(data_point.id)
        return payload

    @staticmethod
    def _merge_belongs_to_set(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        existing_names = _node_set_names(existing.get("belongs_to_set"))
        incoming_names = _node_set_names(incoming.get("belongs_to_set"))
        return {
            **incoming,
            "belongs_to_set": list(dict.fromkeys(existing_names + incoming_names)),
        }

    async def _upsert_collection(
        self,
        collection_name: str,
        points: list[tuple[str, dict[str, Any], list[float]]],
    ) -> None:
        if not points:
            return
        async with self._collection_lock(collection_name):
            client = self.get_qdrant_client()
            try:
                existing_rows = await client.retrieve(
                    collection_name=collection_name,
                    ids=[point_id for point_id, _, _ in points],
                    with_payload=True,
                    with_vectors=False,
                )
                existing = {
                    str(row.id): dict(row.payload or {})
                    for row in existing_rows
                }
                deduped: dict[str, tuple[dict[str, Any], list[float]]] = {}
                for point_id, payload, vector in points:
                    prior_payload = deduped.get(point_id, (existing.get(point_id, {}), vector))[0]
                    deduped[point_id] = (
                        self._merge_belongs_to_set(prior_payload, payload),
                        vector,
                    )
                await client.upsert(
                    collection_name=collection_name,
                    points=[
                        models.PointStruct(
                            id=point_id,
                            payload=payload,
                            vector={"text": vector},
                        )
                        for point_id, (payload, vector) in deduped.items()
                    ],
                    wait=True,
                )
            except Exception as exc:
                raise QdrantProviderError("Qdrant point upsert failed") from exc
            finally:
                await self._close_client(client)

    async def create_data_points(
        self,
        collection_name: str,
        data_points: list[DataPoint],
    ) -> None:
        scope = self._scope({"write"})
        if not data_points:
            return
        await self.create_collection(collection_name)
        vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(point) for point in data_points]
        )
        scoped_points: list[tuple[str, dict[str, Any], list[float]]] = []
        for point, vector in zip(data_points, vectors, strict=True):
            point_id = str(self.stored_id(point.id, dataset=scope.dataset))
            scoped_points.append(
                (
                    point_id,
                    self._payload(
                        point,
                        logical_collection=collection_name,
                        dataset=scope.dataset,
                    ),
                    vector,
                )
            )
        await self._upsert_collection(
            physical_collection_name(self.generation_id, scope.dataset, collection_name),
            scoped_points,
        )

    async def create_vector_index(self, index_name: str, index_property_name: str) -> None:
        await self.create_collection(f"{index_name}_{index_property_name}")

    async def index_data_points(
        self,
        index_name: str,
        index_property_name: str,
        data_points: list[DataPoint],
    ) -> None:
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            [
                IndexSchema(
                    id=point.id,
                    text=DataPoint.get_embeddable_data(point),
                    document_id=getattr(point, "document_id", None),
                    document_name=getattr(point, "document_name", None),
                    chunk_index=getattr(point, "chunk_index", None),
                    source_chunk_id=getattr(point, "source_chunk_id", None),
                    importance_weight=getattr(point, "importance_weight", None),
                    metadata=dict(getattr(point, "metadata", {}) or {}),
                    belongs_to_set=_node_set_names(getattr(point, "belongs_to_set", None)),
                    source_pipeline=getattr(point, "source_pipeline", None),
                    source_task=getattr(point, "source_task", None),
                    source_node_set=getattr(point, "source_node_set", None),
                    source_user=getattr(point, "source_user", None),
                    source_content_hash=getattr(point, "source_content_hash", None),
                )
                for point in data_points
            ],
        )

    def _scored_results(self, points: Sequence[Any]) -> list[ScoredResult]:
        results: list[ScoredResult] = []
        for point in points:
            payload = None
            raw_id = str(point.id)
            if getattr(point, "payload", None) is not None:
                raw_id = str(point.payload.get("citadel_original_id", point.id))
                payload = _serialize({**point.payload, "id": raw_id})
            similarity = getattr(point, "score", None)
            distance = 0.0 if similarity is None else 1.0 - float(similarity)
            results.append(
                ScoredResult(
                    id=parse_id(raw_id),
                    payload=payload,
                    score=distance,
                )
            )
        return results

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int | None = 15,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
        node_name_filter_operator: str = "OR",
    ) -> list[ScoredResult]:
        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()
        scope = self._scope({"read"})
        physical = physical_collection_name(
            self.generation_id, scope.dataset, collection_name
        )
        if not await self._collection_exists(physical):
            return []
        if query_vector is None:
            query_vector = (await self.embed_data([str(query_text)]))[0]
        client = self.get_qdrant_client()
        try:
            if limit is None:
                count = await client.count(
                    collection_name=physical,
                    count_filter=self._scope_filter(scope.dataset),
                    exact=True,
                )
                limit = int(count.count)
            if limit <= 0:
                return []
            response = await client.query_points(
                collection_name=physical,
                query=query_vector,
                using="text",
                query_filter=self._scope_filter(
                    scope.dataset,
                    node_names=node_name,
                    node_name_filter_operator=node_name_filter_operator,
                ),
                limit=limit,
                with_vectors=with_vector,
                with_payload=include_payload,
            )
            return self._scored_results(response.points)
        except Exception as exc:
            raise QdrantProviderError("Qdrant search failed") from exc
        finally:
            await self._close_client(client)

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
    ) -> list[list[ScoredResult]]:
        scope = self._scope({"read"})
        physical = physical_collection_name(
            self.generation_id, scope.dataset, collection_name
        )
        if not await self._collection_exists(physical):
            return [[] for _ in query_texts]
        if not query_texts:
            return []
        vectors = await self.embed_data(query_texts)
        client = self.get_qdrant_client()
        try:
            if limit is None:
                count = await client.count(
                    collection_name=physical,
                    count_filter=self._scope_filter(scope.dataset),
                    exact=True,
                )
                limit = int(count.count)
            if limit <= 0:
                return [[] for _ in query_texts]
            responses = await client.query_batch_points(
                collection_name=physical,
                requests=[
                    models.QueryRequest(
                        query=vector,
                        using="text",
                        filter=self._scope_filter(
                            scope.dataset,
                            node_names=node_name,
                        ),
                        limit=limit,
                        with_vector=with_vectors,
                        with_payload=include_payload,
                    )
                    for vector in vectors
                ],
            )
            return [self._scored_results(response.points) for response in responses]
        except Exception as exc:
            raise QdrantProviderError("Qdrant batch search failed") from exc
        finally:
            await self._close_client(client)

    async def retrieve(
        self,
        collection_name: str,
        data_point_ids: list[str],
    ) -> list[ScoredResult]:
        scope = self._scope({"read"})
        if not data_point_ids:
            return []
        physical = physical_collection_name(
            self.generation_id, scope.dataset, collection_name
        )
        if not await self._collection_exists(physical):
            return []
        client = self.get_qdrant_client()
        try:
            records: list[Any] = []
            for start in range(0, len(data_point_ids), 256):
                raw_batch = data_point_ids[start : start + 256]
                batch = [self.stored_id(point_id, dataset=scope.dataset) for point_id in raw_batch]
                page, _ = await client.scroll(
                    collection_name=physical,
                    scroll_filter=self._scope_filter(scope.dataset, point_ids=batch),
                    limit=len(batch),
                    with_payload=True,
                    with_vectors=False,
                )
                records.extend(page)
            by_id = {str(record.id): record for record in records}
            ordered_ids = [
                str(self.stored_id(point_id, dataset=scope.dataset))
                for point_id in data_point_ids
            ]
            return self._scored_results(
                [by_id[point_id] for point_id in ordered_ids if point_id in by_id]
            )
        except Exception as exc:
            raise QdrantProviderError("Qdrant retrieve failed") from exc
        finally:
            await self._close_client(client)

    async def delete_data_points(
        self,
        collection_name: str,
        data_point_ids: list[UUID],
    ) -> Any:
        scope = self._scope({"write"})
        if not data_point_ids:
            return None
        physical = physical_collection_name(
            self.generation_id, scope.dataset, collection_name
        )
        if not await self._collection_exists(physical):
            return None
        client = self.get_qdrant_client()
        try:
            stored_ids = [
                self.stored_id(point_id, dataset=scope.dataset)
                for point_id in data_point_ids
            ]
            return await client.delete(
                collection_name=physical,
                points_selector=models.FilterSelector(
                    filter=self._scope_filter(scope.dataset, point_ids=stored_ids)
                ),
                wait=True,
            )
        except Exception as exc:
            raise QdrantProviderError("Qdrant delete failed") from exc
        finally:
            await self._close_client(client)

    async def count_data_points(self, collection_name: str, *, exact: bool = True) -> int:
        scope = self._scope({"read", "admin"})
        physical = physical_collection_name(
            self.generation_id, scope.dataset, collection_name
        )
        if not await self._collection_exists(physical):
            return 0
        client = self.get_qdrant_client()
        try:
            result = await client.count(
                collection_name=physical,
                count_filter=self._scope_filter(scope.dataset),
                exact=exact,
            )
            return int(result.count)
        except Exception as exc:
            raise QdrantProviderError("Qdrant count failed") from exc
        finally:
            await self._close_client(client)

    async def scroll_data_points(
        self,
        collection_name: str,
        *,
        offset: str | int | UUID | None = None,
        limit: int = 256,
        with_vectors: bool = False,
    ) -> tuple[list[ScoredResult], str | int | UUID | None]:
        scope = self._scope({"read", "admin"})
        if limit <= 0:
            raise ValueError("scroll limit must be positive")
        physical = physical_collection_name(
            self.generation_id, scope.dataset, collection_name
        )
        if not await self._collection_exists(physical):
            return [], None
        client = self.get_qdrant_client()
        try:
            page, next_offset = await client.scroll(
                collection_name=physical,
                scroll_filter=self._scope_filter(scope.dataset),
                offset=offset,
                limit=limit,
                with_payload=True,
                with_vectors=with_vectors,
            )
            return self._scored_results(page), next_offset
        except Exception as exc:
            raise QdrantProviderError("Qdrant scroll failed") from exc
        finally:
            await self._close_client(client)

    async def get_collection_names(self) -> list[str]:
        self._scope({"read", "write", "admin"})
        prefix = f"citadel_g_{_hash(self.generation_id)}_"
        client = self.get_qdrant_client()
        try:
            response = await client.get_collections()
            return sorted(
                collection.name
                for collection in response.collections
                if collection.name.startswith(prefix)
            )
        except Exception as exc:
            raise QdrantProviderError("Qdrant collection listing failed") from exc
        finally:
            await self._close_client(client)

    async def remove_belongs_to_set_tags(
        self,
        tags: list[str],
        node_ids: list[str] | None = None,
    ) -> None:
        scope = self._scope({"write"})
        target_tags = {str(tag) for tag in tags if str(tag).strip()}
        if not target_tags or node_ids == []:
            return
        id_scope = (
            {
                str(self.stored_id(node_id, dataset=scope.dataset))
                for node_id in node_ids
            }
            if node_ids is not None
            else None
        )
        for collection_name in await self.get_collection_names():
            client = self.get_qdrant_client()
            try:
                offset: str | int | UUID | None = None
                while True:
                    page, offset = await client.scroll(
                        collection_name=collection_name,
                        scroll_filter=self._scope_filter(scope.dataset),
                        offset=offset,
                        limit=256,
                        with_payload=True,
                        with_vectors=False,
                    )
                    for record in page:
                        point_id = str(record.id)
                        if id_scope is not None and point_id not in id_scope:
                            continue
                        payload = dict(record.payload or {})
                        existing_tags = _node_set_names(payload.get("belongs_to_set"))
                        if not target_tags.intersection(existing_tags):
                            continue
                        remaining = [tag for tag in existing_tags if tag not in target_tags]
                        if remaining:
                            await client.set_payload(
                                collection_name=collection_name,
                                payload={"belongs_to_set": remaining},
                                points=models.FilterSelector(
                                    filter=self._scope_filter(
                                        scope.dataset, point_ids=[point_id]
                                    )
                                ),
                                wait=True,
                            )
                        else:
                            await client.delete(
                                collection_name=collection_name,
                                points_selector=models.FilterSelector(
                                    filter=self._scope_filter(
                                        scope.dataset, point_ids=[point_id]
                                    )
                                ),
                                wait=True,
                            )
                    if offset is None:
                        break
            except Exception as exc:
                raise QdrantProviderError("Qdrant NodeSet detag failed") from exc
            finally:
                await self._close_client(client)

    async def upsert_raw_vectors(
        self,
        collection_name: str,
        points: list[dict],
        payload_schema: Any = None,
    ) -> None:
        del payload_schema
        scope = self._scope({"write"})
        expected_dimension = self.embedding_engine.get_vector_size()
        normalized: list[tuple[str, dict[str, Any], list[float]]] = []
        for point in points:
            point_id = str(point.get("id", "")).strip()
            vector = point.get("vector")
            if not point_id or not isinstance(vector, list):
                raise ValueError("raw Qdrant point requires id and vector")
            if len(vector) != expected_dimension:
                raise ValueError(
                    f"raw Qdrant vector dimension {len(vector)} does not match {expected_dimension}"
                )
            payload = dict(point.get("payload") or {})
            normalized.append((point_id, payload, [float(value) for value in vector]))
        await self.create_collection(collection_name)

        def stamp(raw_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {
                **_serialize(payload),
                _GENERATION_FIELD: self.generation_id,
                _SCOPE_DATASET_FIELD: scope.dataset,
                _LOGICAL_COLLECTION_FIELD: collection_name,
                "citadel_original_id": raw_id,
            }

        await self._upsert_collection(
            physical_collection_name(self.generation_id, scope.dataset, collection_name),
            [
                (
                    str(self.stored_id(point_id, dataset=scope.dataset)),
                    stamp(point_id, payload),
                    vector,
                )
                for point_id, payload, vector in normalized
            ],
        )

    async def prune(self) -> None:
        scope = self._scope({"write", "admin"})
        collection_names = await self.get_collection_names()
        client = self.get_qdrant_client()
        try:
            for collection_name in collection_names:
                await client.delete(
                    collection_name=collection_name,
                    points_selector=models.FilterSelector(
                        filter=self._scope_filter(scope.dataset)
                    ),
                    wait=True,
                )
        except Exception as exc:
            raise QdrantProviderError("Qdrant dataset prune failed") from exc
        finally:
            await self._close_client(client)

    async def run_migrations(self) -> None:
        return None

    async def close(self) -> None:
        return None


def register_qdrant_adapter() -> None:
    installed = version("qdrant-client")
    if installed != QDRANT_CLIENT_VERSION:
        raise QdrantConfigurationError(
            f"qdrant-client {installed} is installed; Citadel requires {QDRANT_CLIENT_VERSION}"
        )
    if os.getenv("ENABLE_BACKEND_ACCESS_CONTROL", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise QdrantConfigurationError(
            "Citadel Qdrant adapter requires ENABLE_BACKEND_ACCESS_CONTROL=true"
        )
    if os.getenv("VECTOR_DATASET_DATABASE_HANDLER", "").strip().lower() != "qdrant":
        raise QdrantConfigurationError(
            "Citadel Qdrant adapter requires VECTOR_DATASET_DATABASE_HANDLER=qdrant"
        )
    _required_text(os.getenv("CITADEL_GENERATION_ID", ""), "generation scope")
    from cognee.infrastructure.databases.dataset_database_handler import (
        use_dataset_database_handler,
    )
    from cognee.infrastructure.databases.vector import use_vector_adapter

    use_vector_adapter("qdrant", CitadelQdrantAdapter)
    use_dataset_database_handler(
        "qdrant",
        CitadelQdrantDatasetDatabaseHandler,
        "qdrant",
    )
