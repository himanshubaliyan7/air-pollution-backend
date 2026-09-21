from fastapi import APIRouter
from pydantic import BaseModel

from common.attributions import get_attributions

router = APIRouter(tags=["attributions"])


class AttributionOut(BaseModel):
    id: str
    text: str  # show exactly as returned
    url: str | None
    required: bool  # true: a licence requires showing it; false: a courtesy credit
    applies_to: str


@router.get("/attributions", response_model=list[AttributionOut])
def list_attributions():
    """Data credits to display (e.g. in a page footer) on every screen. Required ones are
    a licence condition; the current-aqi response also carries the CPCB text next to the
    readings it covers."""
    return [AttributionOut(**a.__dict__) for a in get_attributions()]
