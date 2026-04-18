from fastapi import APIRouter, HTTPException

from app.schemas.agent import AgentRequest, AgentResponse, AgentUserRequest
from app.services.agent_service import get_agent_metrics, get_proactive_recommendations, run_agent

router = APIRouter(prefix="/agent", tags=["Agent"])


@router.post("/run", response_model=AgentResponse)
def run_agent_route(request: AgentRequest):
    try:
        return run_agent(request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Agent execution failed: {exc}") from exc


@router.post("/metrics")
def agent_metrics_route(request: AgentUserRequest):
    try:
        return get_agent_metrics(request.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Metrics generation failed: {exc}") from exc


@router.post("/proactive-check")
def proactive_check_route(request: AgentUserRequest):
    try:
        return get_proactive_recommendations(request.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Proactive check failed: {exc}") from exc
