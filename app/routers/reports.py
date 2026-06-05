"""Reports router — generates and streams the weekly Word report."""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from datetime import datetime

from app.database import get_db
from app.services.report_service import generate_weekly_report

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/weekly")
def weekly_report(db: Session = Depends(get_db)):
    """
    Generate and return the weekly Platform Performance & Cost Optimization
    report as a downloadable .docx file.
    """
    buf = generate_weekly_report(db)
    filename = f"platform_optimization_weekly_{datetime.utcnow().strftime('%Y%m%d')}.docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
