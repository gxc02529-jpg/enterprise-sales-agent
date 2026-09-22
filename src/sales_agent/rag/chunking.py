from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sales_agent.contracts import DocumentIngestRequest, Principal


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    document_id: str
    tenant_id: str
    owner_user_id: str
    title: str
    document_type: str
    text: str
    customer_ids: list[str]
    permission_tags: list[str]
    source_uri: str
    version: str
    chunk_index: int
    content_hash: str

    def as_milvus_row(self, dense_vector: list[float]) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "title": self.title,
            "document_type": self.document_type,
            "text": self.text,
            "customer_ids": self.customer_ids,
            "permission_tags": self.permission_tags,
            "source_uri": self.source_uri,
            "version": self.version,
            "chunk_index": self.chunk_index,
            "content_hash": self.content_hash,
            "dense_vector": dense_vector,
        }


def normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _units(text: str, max_chars: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            units.append(paragraph)
            continue
        sentences = [
            item.strip()
            for item in re.split(r"(?<=[。！？；.!?;])\s*", paragraph)
            if item.strip()
        ]
        for sentence in sentences or [paragraph]:
            if len(sentence) <= max_chars:
                units.append(sentence)
            else:
                units.extend(
                    sentence[start : start + max_chars]
                    for start in range(0, len(sentence), max_chars)
                )
    return units


def chunk_document(
    principal: Principal,
    document: DocumentIngestRequest,
    *,
    chunk_size_chars: int,
    overlap_chars: int,
) -> tuple[str, list[DocumentChunk]]:
    text = normalize_text(document.text)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    chunks: list[str] = []
    current = ""
    for unit in _units(text, chunk_size_chars):
        candidate = f"{current}\n\n{unit}".strip() if current else unit
        if len(candidate) <= chunk_size_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            prefix = current[-overlap_chars:] if overlap_chars else ""
            current = f"{prefix}\n\n{unit}".strip()
        else:
            current = unit
        while len(current) > chunk_size_chars:
            chunks.append(current[:chunk_size_chars])
            start = chunk_size_chars - overlap_chars if overlap_chars else chunk_size_chars
            current = current[start:]
    if current:
        chunks.append(current)

    owner = document.owner_user_id or principal.user_id
    records = []
    for index, chunk_text in enumerate(chunks):
        identity = f"{principal.tenant_id}:{document.document_id}:{content_hash}:{index}"
        chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        records.append(
            DocumentChunk(
                chunk_id=chunk_id,
                document_id=document.document_id,
                tenant_id=principal.tenant_id,
                owner_user_id=owner,
                title=document.title,
                document_type=document.document_type,
                text=chunk_text,
                customer_ids=list(dict.fromkeys(document.customer_ids)),
                permission_tags=list(dict.fromkeys(document.permission_tags)),
                source_uri=document.source_uri or "",
                version=document.version,
                chunk_index=index,
                content_hash=content_hash,
            )
        )
    return content_hash, records
