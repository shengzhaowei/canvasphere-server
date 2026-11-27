"""Canonical Protocol Profiles for the multimodal model platform.

Pure data: profile_key / version / modality / billing_metric /
route_template / description for every upstream adapter family.
Kept in its own module so `app.byok` can import it without pulling in
`app.model_platform` (which imports `byok`) — previously that cycle
crashed the ARQ worker at import time.
"""
from __future__ import annotations

from typing import Any


PROTOCOL_PROFILES: list[dict[str, Any]] = [
    {
        "profile_key": "openai_compatible_chat",
        "version": "v1",
        "modality": "text",
        "billing_metric": "per_million_tokens",
        "route_template": "/v1/chat/completions",
        "description": "OpenAI-compatible chat completions & tool calling",
    },
    {
        "profile_key": "openlux_gpt_images",
        "version": "v1",
        "modality": "image",
        "billing_metric": "per_image",
        "route_template": "/v1/images/generations",
        "description": "GPT Image 2 / 2.5 / 2.6 generation & editing",
    },
    {
        "profile_key": "ark_images",
        "version": "v3",
        "modality": "image",
        "billing_metric": "per_image",
        "route_template": "/api/v3/images/generations",
        "description": "Volcengine Ark Seedream image generation",
    },
    {
        "profile_key": "qwen_images",
        "version": "v1",
        "modality": "image",
        "billing_metric": "per_image",
        "route_template": "/v1/qwen/images/generations",
        "description": "Qwen Image 3.0 / Pro image generation",
    },
    {
        "profile_key": "gemini_images",
        "version": "v1",
        "modality": "image",
        "billing_metric": "per_image",
        "route_template": "/v1/gemini/images/generations",
        "description": "Gemini Nano Banana image generation",
    },
    {
        "profile_key": "midjourney_proxy",
        "version": "v2",
        "modality": "image",
        "billing_metric": "per_image",
        "route_template": "/mj/submit/imagine",
        "description": "Midjourney V7 / V8.2 / Niji imagine + U/V/upscale/zoom/pan actions",
    },
    {
        "profile_key": "zongheng_video",
        "version": "v1",
        "modality": "video",
        "billing_metric": "per_second_or_call",
        "route_template": "/v1/video/generations",
        "description": "Seedance 2.0 / 2.0 Fast / 2.5 Pro multimodal video generation",
    },
    {
        "profile_key": "minimax_video",
        "version": "v1",
        "modality": "video",
        "billing_metric": "per_second_or_call",
        "route_template": "/v1/minimax/video_generation",
        "description": "MiniMax H3 video generation (768P / 2K)",
    },
    {
        "profile_key": "wan_video",
        "version": "v1",
        "modality": "video",
        "billing_metric": "per_second",
        "route_template": "/v1/wan/video/generations",
        "description": "Alibaba Wan 3.0 video generation (480P / 720P / 1080P)",
    },
    {
        "profile_key": "grok_video",
        "version": "v1",
        "modality": "video",
        "billing_metric": "per_second",
        "route_template": "/v1/grok/video/generations",
        "description": "Grok video generation (up to 6 reference images)",
    },
    {
        "profile_key": "minimax_tts",
        "version": "v2",
        "modality": "tts",
        "billing_metric": "per_call",
        "route_template": "/v1/t2a_v2",
        "description": "MiniMax Speech 2.8 HD/Turbo TTS, voice cloning & voice design",
    },
    {
        "profile_key": "minimax_asr",
        "version": "v1",
        "modality": "asr",
        "billing_metric": "per_second",
        "route_template": "/v1/audio/transcriptions",
        "description": "MiniMax ASR 1.0 audio transcription",
    },
    {
        "profile_key": "suno_music",
        "version": "v6",
        "modality": "music",
        "billing_metric": "per_operation",
        "route_template": "/v1/suno/generate",
        "description": "Suno v6 / Wild / Mini music workbench with 6 modes and 20 operations",
    },
]
