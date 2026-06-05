"""Upload router — file upload and ingestion to ChromaDB."""
import uuid
from typing import List
from fastapi import APIRouter, UploadFile, File, HTTPException, status
from loguru import logger
import PyPDF2
import tempfile
import os
import re


from app.services.vector_store import get_vector_store
from app.services.mistral_service import mistral_service

router = APIRouter(prefix="/api/upload", tags=["upload"])


def extract_text_from_file(file_content: bytes, filename: str) -> str:
    """Extract text from uploaded file based on extension."""
    ext = filename.lower().split('.')[-1]
    
    if ext == 'txt':
        return file_content.decode('utf-8', errors='ignore')
    
    elif ext == 'pdf':
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
                tmp.write(file_content)
                tmp_path = tmp.name
            
            text = []
            with open(tmp_path, 'rb') as pdf_file:
                reader = PyPDF2.PdfReader(pdf_file)
                for page_num, page in enumerate(reader.pages):
                    page_text = page.extract_text()
                    if page_text:
                        text.append(f"[Page {page_num + 1}]\n{page_text}")
            
            os.unlink(tmp_path)
            return "\n\n".join(text)
        except Exception as e:
            logger.error(f"PDF extraction failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to extract PDF: {str(e)}"
            )
    
    elif ext == 'docx':
        try:
            from docx import Document
            with tempfile.NamedTemporaryFile(delete=False, suffix='.docx') as tmp:
                tmp.write(file_content)
                tmp_path = tmp.name
            
            doc = Document(tmp_path)
            text = [para.text for para in doc.paragraphs if para.text.strip()]
            os.unlink(tmp_path)
            return "\n\n".join(text)
        except Exception as e:
            logger.error(f"DOCX extraction failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to extract DOCX: {str(e)}"
            )
    
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {ext}. Supported: txt, pdf, docx"
        )


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        
        if chunk.strip():
            chunks.append(chunk)
        
        start += chunk_size - overlap
    
    return chunks


def normalize_cloud_platform(platform: str | None) -> str:
    if not platform:
        return "unknown"
    normalized = platform.strip().lower()
    if normalized in {"aws", "amazon", "amazon web services", "amazonwebservices"}:
        return "aws"
    if normalized in {"azure", "microsoft", "microsoft azure", "msft"}:
        return "azure"
    if normalized in {"gcp", "google cloud", "google cloud platform", "google"}:
        return "gcp"
    return normalized or "unknown"


def detect_cloud_platform(text: str) -> str:
    lower = text.lower()
    providers = {
        "aws": [
            "aws", "amazon web services", "amazon", "s3", "ec2", "lambda",
            "cloudwatch", "rds", "dynamodb", "vpc", "elastic beanstalk", "ecs", "eks"
        ],
        "azure": [
            "azure", "microsoft azure", "msft", "resource group", "cosmos db",
            "app service", "functions", "storage account", "service bus", "sql database",
            "virtual machines", "vmss", "aks"
        ],
        "gcp": [
            "gcp", "google cloud", "compute engine", "bigquery", "cloud storage",
            "gke", "cloud run", "pub/sub", "cloud functions", "datastore"
        ],
    }

    def count_matches(tokens: list[str]) -> int:
        count = 0
        for token in tokens:
            if re.search(rf"\b{re.escape(token)}\b", lower):
                count += 1
        return count

    scores = {provider: count_matches(tokens) for provider, tokens in providers.items()}
    best_provider = max(scores, key=scores.get)
    if scores[best_provider] > 0:
        return best_provider
    return "unknown"


def score_document_relevance(text: str, threshold: float = 75.0) -> dict:
    """Use the LLM to score a document's relevance and return a numeric score."""
    sample = text.strip()
    if len(sample) > 4000:
        sample = sample[:4000] + "\n\n...[truncated for scoring]"

    system_prompt = (
        "You are an expert assistant that evaluates whether a document is relevant to platform optimization, cloud cost optimization, "
        "performance improvement, or infrastructure efficiency. Return only JSON with keys: relevance_score, verdict, and reason. "
        "The relevance_score should be a number between 0 and 100. The verdict should be 'accept' if the score is greater than or equal to the threshold, otherwise 'reject'."
    )

    user_prompt = (
        f"Score the following document for relevance to platform optimization and cloud efficiency. "
        f"Respond with JSON only. Threshold={threshold}%.\n\nDocument:\n{sample}"
    )

    response = mistral_service.chat_json(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        tier="efficient",
        max_tokens=300,
    )

    if response.get("_error"):
        raise RuntimeError("LLM scoring failed: " + str(response.get("_raw", response)))

    score = response.get("relevance_score")
    if score is None:
        raise RuntimeError("LLM scoring did not return a relevance_score")

    if isinstance(score, str):
        score = score.strip().rstrip("%")
    try:
        score = float(score)
    except Exception as e:
        raise RuntimeError(f"Invalid relevance_score from LLM: {score} ({e})")

    score = max(0.0, min(100.0, score))
    verdict = response.get("verdict") or ("accept" if score >= threshold else "reject")
    reason = response.get("reason") or response.get("analysis") or "No reason provided."

    return {
        "relevance_score": score,
        "verdict": verdict,
        "reason": reason,
        "threshold": threshold,
        "accepted": score >= threshold,
    }


@router.post("/document")
async def upload_document(
    file: UploadFile = File(...),
    collection: str = "semantic",
    threshold: float = 75.0,
    cloud_platform: str | None = None,
):
    """
    Upload and ingest a document into ChromaDB.
    
    - **file**: Document file (txt, pdf, or docx)
    - **collection**: Target collection (playbooks, episodic, or semantic)
    """
    if collection not in ["playbooks", "episodic", "semantic"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collection must be: playbooks, episodic, or semantic"
        )
    
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File name is required"
        )
    
    try:
        # Read file content
        content = await file.read()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File is empty"
            )
        
        # Extract text
        text = extract_text_from_file(content, file.filename)
        if not text.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not extract text from file"
            )

        clean_cloud_platform = normalize_cloud_platform(cloud_platform)
        if clean_cloud_platform == "unknown":
            clean_cloud_platform = detect_cloud_platform(text)

        # Score relevance with LLM before ingesting
        analysis = score_document_relevance(text, threshold=threshold)
        if not analysis["accepted"]:
            return {
                "status": "rejected",
                "filename": file.filename,
                "collection": collection,
                "cloud_platform": clean_cloud_platform,
                "relevance_score": analysis["relevance_score"],
                "threshold": threshold,
                "verdict": analysis["verdict"],
                "reason": analysis["reason"],
                "message": "Document did not meet the relevance threshold and was not uploaded.",
            }

        # Chunk text
        chunks = chunk_text(text)
        if not chunks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Text extraction resulted in no content"
            )
        
        # Ingest to ChromaDB
        vector_store = get_vector_store()
        
        metadatas = [
            {
                "source": file.filename,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "relevance_score": analysis["relevance_score"],
                "relevance_verdict": analysis["verdict"],
                "cloud_platform": clean_cloud_platform,
            }
            for i in range(len(chunks))
        ]
        
        if collection == "playbooks":
            ids = vector_store.add_playbook_chunks(chunks, metadatas)
        elif collection == "episodic":
            ids = []
            for chunk, metadata in zip(chunks, metadatas):
                id_ = vector_store.add_episodic_memory(chunk, metadata)
                ids.append(id_)
        else:  # semantic
            ids = []
            for chunk, metadata in zip(chunks, metadatas):
                id_ = vector_store.add_semantic_memory(chunk, metadata)
                ids.append(id_)
        
        logger.info(
            f"Ingested {len(chunks)} chunks from {file.filename} into {collection}"
        )
        
        return {
            "status": "success",
            "filename": file.filename,
            "collection": collection,
            "cloud_platform": clean_cloud_platform,
            "relevance_score": analysis["relevance_score"],
            "threshold": threshold,
            "verdict": analysis["verdict"],
            "reason": analysis["reason"],
            "chunks_ingested": len(chunks),
            "document_ids": ids,
            "message": f"Successfully ingested {len(chunks)} chunks"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upload failed: {str(e)}"
        )


@router.get("/status")
async def upload_status():
    """Get current ChromaDB collection counts."""
    try:
        vector_store = get_vector_store()
        counts = vector_store.counts()
        return {
            "status": "ok",
            "collections": counts,
            "total_documents": sum(counts.values())
        }
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
