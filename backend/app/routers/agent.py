from fastapi import APIRouter, HTTPException

from app.schemas.agent import AgentRequest, AgentResponse
from app.services.agent_service import run_agent

router = APIRouter(prefix="/agent", tags=["Agent"])


@router.post("/run", response_model=AgentResponse)
def run_agent_route(request: AgentRequest):
    try:
        return run_agent(request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Agent execution failed: {exc}") from exc
