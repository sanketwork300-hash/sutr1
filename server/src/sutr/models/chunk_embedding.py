import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, LargeBinary, SQLModel


class ChunkEmbedding(SQLModel, table=True):
    """A vector for one chunk, from one embedding model.

    Keyed by `(chunk, model)` rather than by chunk alone: changing the
    embedding model changes what the numbers mean, and mixing two models'
    vectors in one similarity search produces confident nonsense.

    Stored as raw float32 bytes. The LLD prefers Qdrant (§5.4); this is the
    fallback that keeps semantic retrieval possible without it (ADR-015), and
    the place Qdrant is populated from when it exists.

    `dimensions` is recorded because a search that compares vectors of
    different lengths is a bug worth catching at read time rather than
    producing a meaningless number.
    """

    __tablename__ = "chunk_embedding"
    __table_args__ = (UniqueConstraint("chunk_id", "model", name="uq_chunk_embedding_model"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    document_id: uuid.UUID = Field(foreign_key="document.id", index=True)
    chunk_id: uuid.UUID = Field(foreign_key="document_chunk.id", index=True)

    model: str = Field(index=True)
    dimensions: int = Field(default=0)
    vector: bytes = Field(sa_type=LargeBinary)
    # Precomputed, so cosine similarity is a dot product at query time.
    norm: float = Field(default=0.0)

    created_at: datetime = Field(default_factory=datetime.utcnow)
