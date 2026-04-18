from groq import Groq
import os
from pathlib import Path
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = APP_DIR.parent
load_dotenv(BACKEND_DIR / ".env")
load_dotenv(APP_DIR / ".env")

client = None

MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.1-8b-instant")
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("MODEL_FALLBACKS", "llama-3.1-8b-instant,llama3-8b-8192").split(",")
    if m.strip()
]


def _get_client() -> Groq:
    global client
    if client is not None:
        return client

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing. Add it to backend/.env or backend/app/.env")

    client = Groq(api_key=api_key)
    return client


def _try_chat_completion(system_prompt: str, user_prompt: str, temperature: float = 0.6, max_tokens: int = 700) -> str:
    groq_client = _get_client()
    models_to_try = [MODEL_NAME] + [m for m in FALLBACK_MODELS if m != MODEL_NAME]
    last_error = None

    for model in models_to_try:
        try:
            response = groq_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            print("🧠 MODEL USED:", model)
            return response.choices[0].message.content.strip()
        except Exception as exc:
            last_error = exc
            continue

    raise RuntimeError(f"Groq completion failed for all models: {last_error}")


def generate_ai_response(prompt: str):
    print("🔑 GROQ KEY: loaded")

    return _try_chat_completion(
        system_prompt="You are a certified fitness coach. Respond only in valid JSON.",
        user_prompt=prompt,
        temperature=0.6,
        max_tokens=700,
    )


def generate_ai_text_response(prompt: str):
    print("🔑 GROQ KEY: loaded")
    return _try_chat_completion(
        system_prompt="You are Lifeline Coach. Reply in plain conversational text.",
        user_prompt=prompt,
        temperature=0.6,
        max_tokens=700,
    )
