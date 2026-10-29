from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Pahunch API"
    database_url: str = "sqlite:///./pahunch.db"
    gnani_api_key: str = ""
    gnani_base_url: str = "https://api.vachana.ai"
    gnani_stt_model: str = "prisma-v3.0"
    gnani_stt_language: str = "indic-auto"
    gnani_tts_model: str = "timbre-v3.0"
    gnani_tts_voice: str = "Shivani"
    gnani_tts_language: str = "hi-en"
    gnani_timeout_seconds: float = 30.0
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 12.0
    llm_max_tokens: int = 600
    llm_temperature: float = 0.1
    llm_structured_mode: Literal["json_schema", "json_object"] = "json_schema"
    llm_enable_thinking: bool = False
    planner_min_confidence: float = 0.65
    planner_min_transcript_confidence: float = 0.55
    realtime_sample_rate: int = 16_000
    realtime_vad_threshold: int = 500
    realtime_silence_ms: int = 1000
    realtime_max_utterance_ms: int = 15_000
    realtime_min_speech_ms: int = 300
    streaming_stt_enabled: bool = True
    streaming_stt_candidates: str = "hi-IN,en-IN"
    streaming_stt_transcript_timeout_seconds: float = 3.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
