# Lifeline AI Backend

FastAPI backend for Lifeline AI. This service handles:
- Risk scoring from health form inputs
- AI-based wellness analysis
- Workout plan generation
- Nutrition plan generation
- Context-aware coach chat

## Tech Stack

- Python + FastAPI
- Pydantic schemas
- Groq LLM client
- Firebase (Firestore)

## Project Structure

```text
backend/
	app/
		main.py
		core/
			firebase_client.py
			groq_client.py
		routers/
			chat.py
			nutrition.py
			workout.py
		schemas/
			chat.py
			nutrition.py
			predict_schema.py
			workout_schema.py
		services/
			analyze_service.py
			chat_service.py
			nutrition_service.py
			risk_engine.py
			workout_service.py
	requirements.txt
```

## Quick Start

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create a `.env` file in `backend/` with:

```env
GROQ_API_KEY=your_api_key_here
MODEL_NAME=llama-3.1-8b-instant
```

4. Ensure Firebase service account JSON is configured for `firebase_client.py`.

5. Start the API server:

```bash
uvicorn app.main:app --reload --port 8000
```

## API Endpoints

- `GET /` : health message
- `GET /health` : status check
- `POST /predict` : computes risk score and risk level
- `POST /analyze` : AI summary based on user profile
- `POST /workouts/generate` : generates 7-day workout plan
- `POST /nutrition/generate` : generates 7-day nutrition plan
- `POST /chat` : contextual coach response using latest plans
- `POST /agent/run` : closed-loop agent orchestration (observe, log, decide, act)

### Agent Endpoint Contract

Request:

```json
{
	"user_id": "firebase-user-id",
	"message": "ate 2 rotis and paneer, did 25 minutes workout, weight 73.4 kg",
	"mode": "auto",
	"goal": "weight loss",
	"autonomous": false,
	"context": {}
}
```

Response includes:
- `summary`
- `actions`
- `nudges`
- `structured_logs`
- `decision` (drift status, plan refresh decisions, risk snapshot)
- `plan_updates` (workout/nutrition updates, food reality adaptation)
- `weekly_reflection`
- `trace` (observe -> auto_log -> decide -> act)

## Current Flow (High Level)

1. Frontend submits health form to `/predict` and `/analyze`.
2. User can request workout and nutrition plans.
3. Plans are saved under user data in Firestore.
4. Chat endpoint uses latest plans + user message to produce tailored responses.

## Agentic Upgrade Blueprint

### Where the project is today

Current behavior is mostly a stateless generation flow:

Input -> LLM -> Output plan -> Done

This is AI-enabled, but not yet truly agentic.

### What it should become

Target behavior is a closed-loop system:

Observe -> Plan -> Act -> Evaluate -> Adapt -> Repeat

This loop is the core of an agentic wellness product.

### Core upgrade: Adaptive Health Guardian

The product should shift from:

"Here is your diet/workout plan"

to:

"I am continuously tracking your progress, adapting your plan, and nudging you toward your goal."

### Simple multi-agent architecture (lightweight)

1. Planner Agent
- Creates initial workout and nutrition plan
- Breaks plan into daily actionable tasks

2. Monitoring Agent
- Tracks user logs (weight, meals, workouts)
- Detects missing logs and behavior drift

3. Decision Agent
- Decides if plan should change
- Decides if user should receive a nudge
- Decides if progress is slow or off-target

4. Action Agent
- Sends reminders/nudges
- Regenerates partial plans when needed
- Sends risk-aware warnings and coaching notes

### Build principle (important)

Do not start with many agents or heavy infrastructure.

Start with one clean loop and 3-4 key decisions. This is enough to outperform most early projects and is easier to debug.

### Agentic features to implement first

1. Progress drift detection
- Compare expected trend vs actual trend (weight, workouts, compliance)
- If drift is high, adjust calories/workout intensity for the next period

2. Food reality adapter
- Example: user says "Today I only have rice and dal"
- Agent recalculates macros and adjusts the rest of day plan instantly
- This is highly practical for Indian household constraints

3. Behavioral nudging system
- Detect skipped workouts, late meals, low adherence streaks
- Send timely and limited nudges (avoid spam)

4. Auto logging through chat
- Parse free text logs like "ate 2 rotis and paneer"
- Convert to structured meal/workout entries
- Update daily totals and progress state

5. Weekly self-reflection loop
- Summarize weekly adherence and outcomes
- Identify blockers (time, consistency, food availability)
- Adapt next week's strategy

### LangChain and LangGraph fit

- LangChain: tool and model wiring (LLM + tools + memory primitives)
- LangGraph: stateful looping workflow for Observe/Plan/Act/Evaluate cycles

Practical guideline:
- LangChain is component wiring
- LangGraph is orchestration for iterative agent loops

### Proposed implementation path in this backend

Phase 1 (minimal viable agent loop)
- Add unified endpoint: `POST /agent/run`
- Add shared agent state per user/session
- Add intent router and tool executor over existing services
- Return structured trace: observed_state, decision, action, result

Phase 2 (monitoring + nudges)
- Add daily/weekly monitoring jobs
- Add drift score and nudge policy
- Log all agent actions to Firestore for auditability

Phase 3 (adaptive planning)
- Partial plan regeneration (do not rebuild everything every time)
- Weekly strategy updates based on reflection loop

### Product decisions pending

Before final implementation, confirm:

1. Autonomy mode: fully autonomous vs semi-autonomous
2. Data policy: explicit daily logging vs inference-first from chat
3. Delivery mode: web only vs reminder/notification behavior
4. API constraint: model provider and budget/free-tier preference

## Notes

- This project is a wellness assistant prototype.
- Outputs are not medical diagnosis or clinical advice.
