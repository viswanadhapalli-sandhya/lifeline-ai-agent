from fastapi import APIRouter, HTTPException

from app.schemas.agent import (
    AgentProactiveCleanupRequest,
    AgentProactiveRunRequest,
    AgentRequest,
    AgentResponse,
    AgentUserRequest,
)
from app.services.agent_service import get_agent_metrics, get_proactive_recommendations, run_agent
from app.services.proactive_loop_service import (
    run_autonomous_proactive_cycle,
    run_proactive_event_retention_cleanup,
    run_proactive_slot,
)

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


@router.post("/proactive/run-now")
def run_proactive_now_route(request: AgentProactiveRunRequest):
    try:
        return run_proactive_slot(slot=request.slot, user_id=request.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Proactive run failed: {exc}") from exc


@router.post("/proactive/autonomous-run-now")
async def run_autonomous_proactive_now_route(request: AgentUserRequest):
    try:
        return await run_autonomous_proactive_cycle(user_id=request.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Autonomous proactive run failed: {exc}") from exc


@router.post("/proactive/cleanup-run-now")
def run_proactive_cleanup_now_route(request: AgentProactiveCleanupRequest):
    try:
        return run_proactive_event_retention_cleanup(
            user_id=request.user_id,
            retention_days=request.retention_days,
            max_events_per_user=request.max_events_per_user,
            dry_run=request.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Proactive cleanup run failed: {exc}") from exc
