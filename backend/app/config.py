from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Pahunch API"
    database_url: str = "sqlite:///./pahunch.db"
    gnani_api_key: str = ""
    # Extra Gnani keys, comma separated. The provider allows one active
    # request per key, so each extra key adds one concurrent slot.
    gnani_api_keys: str = ""
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
    llm_max_tokens: int = 320
    llm_temperature: float = 0.1
    llm_structured_mode: Literal["json_schema", "json_object"] = "json_schema"
    llm_enable_thinking: bool = False
    planner_min_confidence: float = 0.65
    planner_min_transcript_confidence: float = 0.55
    realtime_sample_rate: int = 16_000
    realtime_vad_threshold: int = 500
    realtime_silence_ms: int = 600
    realtime_quiet_ms: int = 600
    realtime_max_utterance_ms: int = 15_000
    realtime_min_speech_ms: int = 300
    realtime_adaptive_vad: bool = True
    realtime_vad_floor: int = 120
    realtime_vad_ceiling: int = 12_000
    realtime_noise_ratio: float = 3.0
    realtime_calibration_ms: int = 300
    realtime_speech_start_ms: int = 100
    realtime_barge_in_ms: int = 160
    realtime_pre_roll_ms: int = 200
    realtime_no_speech_ms: int = 5_000
    streaming_stt_enabled: bool = True
    streaming_stt_candidates: str = "hi-IN,en-IN"
    streaming_stt_transcript_timeout_seconds: float = 3.0
    dialer_enabled: bool = True
    dialer_interval_seconds: float = Field(default=5, ge=0.1)
    dialer_waiting_timeout_seconds: float = Field(default=120, ge=1)
    dialer_disconnect_grace_seconds: float = Field(default=30, ge=1)
    dialer_idle_timeout_seconds: float = Field(default=300, ge=1)
    # Real phone calls. "none" keeps the browser as the phone. Credentials belong
    # in backend/.env only; none of these has a usable default.
    telephony_provider: Literal["none", "twilio"] = "none"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    # https address Twilio can reach (a tunnel to this server), no trailing slash.
    public_base_url: str = ""
    # The owner's own handset in E.164 form. The dialer may only call this number.
    demo_handset_number: str = ""
    twilio_console_verified: bool = False
    twilio_streaming_verified: bool = False
    gnani_phone_demo_approved: bool = False
    # One approval authorizes only this existing local session, never a future retry.
    twilio_approved_session_id: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
