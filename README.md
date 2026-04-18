# Lifeline AI Agentic

Lifeline AI Agentic is a full-stack wellness coaching system with:

- health risk estimation and AI analysis
- adaptive workout and nutrition planning
- chat-driven agent routing with deterministic intent handling
- proactive coaching loops and event observability
- nutrition shopping intelligence (provider scoring, budget/location awareness, guided checklist)
- hypothetical scenario simulation for "what-if" user queries

This repository includes a FastAPI backend and a React + Vite frontend connected through Firebase (Firestore + Auth).

## Tech Stack

### Backend

- Python 3.13
- FastAPI
- Pydantic
- Firestore (firebase-admin)
- Groq LLM integration
- APScheduler (proactive background jobs)

### Frontend

- React 18
- Vite 5
- Firebase Web SDK
- React Router
- Tailwind CSS

## Repository Structure

```text
lifeline-ai-agentic/
├── backend/
│   ├── app/
│   │   ├── core/                  # Firebase and model clients
│   │   ├── routers/               # FastAPI route modules
│   │   ├── schemas/               # Request/response models
│   │   ├── services/              # Domain and agent services
│   │   └── main.py                # FastAPI app entrypoint
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── context/
│   │   ├── pages/                 # Dashboard, Coach, Nutrition, Insights, etc.
│   │   ├── routes/
│   │   └── services/
│   └── package.json
└── docs/
```

## Core Capabilities

### 1. Health Scoring and Analysis

- `POST /predict`: computes BMI, risk score, and risk level from health form data
- `POST /analyze`: generates AI-based summary/risk interpretation

### 2. Workout and Nutrition Plans

- `POST /workouts/generate`: produces and persists workout plans
- `POST /nutrition/generate`: produces and persists nutrition plans

### 3. Agentic Coach Runtime

- `POST /agent/run`: main agent endpoint
	- deterministic intent detection
	- route-specific handlers (completion, travel, disruption, shopping, progress, scenario simulation, general chat)
	- structured response including decision reasoning and confidence
	- conversation persistence to Firestore

### 4. Proactive Agent Loop

- scheduled proactive runs (morning, afternoon, night)
- on-demand proactive endpoints:
	- `POST /agent/proactive-check`
	- `POST /agent/proactive/run-now`
	- `POST /agent/proactive/autonomous-run-now`
	- `POST /agent/proactive/cleanup-run-now`

### 5. Nutrition Shopping Agent

- pantry sync and missing-item detection
- provider recommendation with best option + alternatives
- budget-aware and city-aware pricing adjustments
- guided shopping progress tracking and follow-up
- consolidated healthcheck endpoint

Key endpoints:

- `POST /nutrition/pantry/sync`
- `POST /nutrition/shopping/plan`
- `POST /nutrition/shopping/proactive-check`
- `POST /nutrition/shopping/progress`
- `POST /nutrition/shopping/followup`
- `POST /nutrition/shopping/adjust-plan`
- `POST /nutrition/shopping/agentic-healthcheck`
- `POST /nutrition/shopping/confirm`

### 6. What-if Scenario Simulation

User questions like:

- "What if I skip workouts?"

are routed to a simulation engine that uses current stats + current plan context to return structured outcome data:

```json
{
	"impact": "delay by 5 days",
	"streak_loss": true,
	"recovery_plan": ["..."]
}
```

This flow is integrated in both:

- `POST /agent/run` (Coach page flow)
- `POST /chat` (contextual chat flow)

### 7. Observability and Explainability

- Agent events are persisted under `users/{uid}/agentEvents`
- timeline now includes explainability fields such as:
	- `why_this_action`
	- `decision_path`
	- `inputs_used`
	- `confidence`

Frontend pages consume these events for transparent decision timelines and coaching suggestions.

## Local Setup

## 1) Clone and open

```bash
git clone <your-repo-url>
cd lifeline-ai-agentic
```

## 2) Backend setup

```bash
cd backend
py -3.13 -m venv ..\.venv
..\.venv\Scripts\activate
pip install -r requirements.txt
```

Create environment files (`backend/.env` and/or `backend/app/.env`) with at least:

```env
GROQ_API_KEY=your_groq_api_key
MODEL_NAME=llama-3.1-8b-instant
```

Ensure Firebase service account credentials are configured for the backend Firebase client.

Start backend:

```bash
cd backend
py -3.13 -m uvicorn app.main:app --reload --port 8000
```

## 3) Frontend setup

```bash
cd frontend
npm install
npm run dev
```

Default frontend dev URL: `http://127.0.0.1:5173`

## 4) Verify service

- `GET http://127.0.0.1:8000/health` should return status UP

## API Snapshot

### Base Health

- `GET /`
- `GET /health`

### Core Analysis

- `POST /predict`
- `POST /analyze`

### Plans

- `POST /workouts/generate`
- `POST /nutrition/generate`

### Chat and Agent

- `POST /chat`
- `POST /agent/run`
- `POST /agent/metrics`
- `POST /agent/proactive-check`
- `POST /agent/proactive/run-now`
- `POST /agent/proactive/autonomous-run-now`
- `POST /agent/proactive/cleanup-run-now`

### Nutrition Shopping

- `POST /nutrition/pantry/sync`
- `POST /nutrition/shopping/plan`
- `POST /nutrition/shopping/proactive-check`
- `POST /nutrition/shopping/progress`
- `POST /nutrition/shopping/followup`
- `POST /nutrition/shopping/adjust-plan`
- `POST /nutrition/shopping/agentic-healthcheck`
- `POST /nutrition/shopping/confirm`

## Example Agent Request

```json
{
	"user_id": "firebase-uid",
	"conversation_id": null,
	"message": "What if I skip workouts for 3 days?",
	"mode": "auto",
	"goal": "general fitness",
	"autonomous": false,
	"context": {}
}
```

## Notes and Safety

- This project is a wellness assistant prototype.
- Recommendations are non-clinical and not medical diagnosis.
- Always add guardrails before production healthcare use.

## Current Status

Implemented and integrated:

- agentic routing with deterministic handlers
- proactive loop + event retention cleanup
- disruption-aware adaptation (fatigue/busy/stress)
- nutrition shopping optimization with guided stateful flow
- what-if scenario simulation in chat/agent runtime
- observability timeline support with decision metadata

