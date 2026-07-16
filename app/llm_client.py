from dotenv import load_dotenv
load_dotenv()

import os
from functools import lru_cache
from typing import Optional

from google import genai
from google.genai import types
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_temperature: float = 0.2
    gemini_max_output_tokens: int = 256

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache(maxsize=1)
def get_settings():
    return Settings()


@lru_cache(maxsize=1)
def get_gemini_client():
    settings = get_settings()
    api_key = settings.gemini_api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Falta GEMINI_API_KEY al fitxer .env")
    return genai.Client(api_key=api_key)


def call_gemini(
    prompt: str,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
):
    settings = get_settings()
    client = get_gemini_client()

    response = client.models.generate_content(
        model=model or settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=temperature if temperature is not None else settings.gemini_temperature,
            max_output_tokens=max_output_tokens if max_output_tokens is not None else settings.gemini_max_output_tokens,
        ),
    )
    return response.text