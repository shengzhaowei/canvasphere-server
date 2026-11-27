"""Seed catalog of 64 enabled RateCards mirroring ai.pecan.wang (Pecan Studio).

Breakdown:
- 4 Text LLMs (per_million_tokens)
- 15 Image models (OpenAI/GPT Image, Qwen, Seedream, Gemini/Nano Banana, Midjourney V7/V8.2/Niji)
- 40 Video model offerings (MiniMax H3, Seedance 2.5 Pro, Seedance 2.0, Seedance 2.0 Fast, Wan 3.0, Grok)
- 4 Audio/Speech models (speech-2.8-hd, speech-2.8-turbo, voice-design-preview, minimax-asr-1.0)
- 1 Suno Music workbench model covering all 20 billed/free operations
Total = 64 enabled RateCards.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RateCard, new_prefixed_id

SUNO_OPERATIONS_PRICING: dict[str, str] = {
    "song_bgm": "71.0000",
    "song_vocals": "71.0000",
    "extend": "71.0000",
    "style_remix": "71.0000",
    "cover": "71.0000",
    "mashup": "71.0000",
    "hum": "71.0000",
    "remaster": "71.0000",
    "replace_lyrics": "71.0000",
    "reference": "71.0000",
    "add_vocals": "71.0000",
    "add_instrumental": "71.0000",
    "crop_extend": "71.0000",
    "full_song_assembly": "81.7000",
    "vocal_instrumental_split": "81.8000",
    "all_stem_separation": "355.2000",
    "sfx": "14.2000",
    "lyrics_gen": "0.0000",
    "expand_style": "0.0000",
    "save_persona": "0.0000",
}

MJ_ACTIONS_MULTIPLIER: dict[str, str] = {
    "imagine": "1.0",
    "u1": "0.25",
    "u2": "0.25",
    "u3": "0.25",
    "u4": "0.25",
    "v1": "1.0",
    "v2": "1.0",
    "v3": "1.0",
    "v4": "1.0",
    "vary_subtle": "1.0",
    "vary_strong": "1.0",
    "reroll": "1.0",
    "upscale_subtle": "0.5",
    "upscale_creative": "0.5",
    "zoom_1.5x": "1.0",
    "zoom_2x": "1.0",
    "pan_left": "1.0",
    "pan_right": "1.0",
    "pan_up": "1.0",
    "pan_down": "1.0",
}

ALL_VIDEO_MODES = ["t2v", "first_frame", "last_frame", "first_last_frame", "multimodal_ref", "draft_extend"]


def _video_card(
    model_key: str,
    display_name: str,
    family: str,
    protocol: str,
    billing_mode: str,
    unit_price: str,
    resolutions: list[str],
    *,
    modes: list[str] | None = None,
    min_sec: int = 5,
    max_sec: int = 15,
    max_images: int = 9,
    max_videos: int = 2,
    max_audios: int = 2,
    ratios: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "model_key": model_key,
        "display_name": display_name,
        "modality": "video",
        "provider_family": family,
        "protocol_profile": protocol,
        "billing_mode": billing_mode,
        "unit_price_credits": unit_price,
        "output_price_credits": None,
        "pricing_meta_json": {"unit": "积分/秒" if billing_mode == "per_second" else "积分/次"},
        "capabilities_json": {
            "resolutions": resolutions,
            "aspect_ratios": ratios or ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21"],
            "guidance_modes": modes or ALL_VIDEO_MODES,
            "min_duration_seconds": min_sec,
            "max_duration_seconds": max_sec,
            "max_ref_images": max_images,
            "max_ref_videos": max_videos,
            "max_ref_audios": max_audios,
        },
    }


def build_seed_rate_cards() -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = [
        # --- 4 Text LLMs ---
        {
            "model_key": "gpt-6-sol",
            "display_name": "GPT 6 Sol",
            "modality": "text",
            "provider_family": "OpenAI",
            "protocol_profile": "openai_compatible_chat",
            "billing_mode": "per_million_tokens",
            "unit_price_credits": "120.0000",
            "output_price_credits": "480.0000",
            "pricing_meta_json": {"unit": "积分/百万Token"},
            "capabilities_json": {"supports_tools": True, "supports_vision": True},
        },
        {
            "model_key": "gpt-6.2-sol",
            "display_name": "GPT 6.2 Sol",
            "modality": "text",
            "provider_family": "OpenAI",
            "protocol_profile": "openai_compatible_chat",
            "billing_mode": "per_million_tokens",
            "unit_price_credits": "180.0000",
            "output_price_credits": "720.0000",
            "pricing_meta_json": {"unit": "积分/百万Token"},
            "capabilities_json": {"supports_tools": True, "supports_vision": True},
        },
        {
            "model_key": "gpt-6.2-astra",
            "display_name": "GPT 6.2 Astra",
            "modality": "text",
            "provider_family": "OpenAI",
            "protocol_profile": "openai_compatible_chat",
            "billing_mode": "per_million_tokens",
            "unit_price_credits": "260.0000",
            "output_price_credits": "1040.0000",
            "pricing_meta_json": {"unit": "积分/百万Token"},
            "capabilities_json": {"supports_tools": True, "supports_vision": True, "reasoning": True},
        },
        {
            "model_key": "deepseek-v4.1-flash",
            "display_name": "DeepSeek V4.1 Flash",
            "modality": "text",
            "provider_family": "DeepSeek",
            "protocol_profile": "openai_compatible_chat",
            "billing_mode": "per_million_tokens",
            "unit_price_credits": "30.0000",
            "output_price_credits": "120.0000",
            "pricing_meta_json": {"unit": "积分/百万Token"},
            "capabilities_json": {"supports_tools": True, "supports_vision": False},
        },
        # --- 15 Image Models ---
        *[
            {
                "model_key": key,
                "display_name": name,
                "modality": "image",
                "provider_family": fam,
                "protocol_profile": proto,
                "billing_mode": "per_image",
                "unit_price_credits": price,
                "output_price_credits": None,
                "pricing_meta_json": (
                    {"unit": "积分/次", "mj_action_multipliers": MJ_ACTIONS_MULTIPLIER}
                    if proto == "midjourney_proxy"
                    else {"unit": "积分/张"}
                ),
                "capabilities_json": (
                    {
                        "max_count": 1,
                        "is_midjourney": True,
                        "prompt_modes": ["manual", "visual", "hybrid"],
                        "grid_count": 4,
                        "actions": list(MJ_ACTIONS_MULTIPLIER.keys()),
                        "aspect_ratios": ["1:1", "16:9", "9:16", "3:2", "2:3", "4:3", "3:4"],
                    }
                    if proto == "midjourney_proxy"
                    else {
                        "max_count": 10,
                        "is_midjourney": False,
                        "sizes": ["auto", "1024x1024", "1536x1024", "1024x1536"],
                        "qualities": ["auto", "low", "medium", "high", "ultra", "max"],
                        "backgrounds": ["auto", "transparent", "opaque"],
                        "formats": ["png", "jpeg", "webp", "gif"],
                    }
                ),
            }
            for key, name, fam, proto, price in [
                ("gpt-image-2.6-flare", "GPT Image 2.6 Flare", "OpenAI", "openlux_gpt_images", "18.0000"),
                ("gpt-image-2.6-sunburst", "GPT Image 2.6 Sunburst", "OpenAI", "openlux_gpt_images", "24.0000"),
                ("gpt-image-2.5-sunburst", "GPT Image 2.5 Sunburst", "OpenAI", "openlux_gpt_images", "16.0000"),
                
                
                
                ("qwen-image-3.0", "qwen-image-3.0", "Qwen", "qwen_images", "12.0000"),
                ("qwen-image-3.0-pro", "qwen-image-3.0-pro", "Qwen", "qwen_images", "18.0000"),
                ("doubao-seedream-5-0-260128", "doubao-seedream-5-0-260128", "Volcengine", "ark_images", "15.0000"),
                
                ("gemini-nano-banana-2.1", "Gemini Nano Banana 2.1", "Google", "gemini_images", "14.0000"),
                
                ("midjourney-v7", "Midjourney-V7", "Midjourney", "midjourney_proxy", "35.0000"),
                ("midjourney-v8.2", "Midjourney-V8.2", "Midjourney", "midjourney_proxy", "42.0000"),
                ("midjourney-niji", "Midjourney-Niji", "Midjourney", "midjourney_proxy", "35.0000"),
            ]
        ],
        # --- 40 Video Models ---
        # MiniMax H3 (3)
        _video_card("minimax-h3-zh-768", "MiniMax H3(zh-768)", "MiniMax", "minimax_video", "per_call", "160.0000", ["768P"], max_sec=10),
        _video_card("minimax-h3-zh-x-2k", "MiniMax H3(zh-x-2k)", "MiniMax", "minimax_video", "per_call", "320.0000", ["2K"], max_sec=10),
        _video_card("minimax-h3-zh-full-768p", "MiniMax H3(zh-全参768p)", "MiniMax", "minimax_video", "per_second", "28.0000", ["768P"], max_sec=15, max_images=12),
        # Seedance 2.5 Pro (13)
        _video_card("seedance-2.5-pro-zh-bb2-480", "Seedance 2.5 Pro(zh-bb2-480)", "Seedance", "zongheng_video", "per_second", "14.0000", ["480P"], max_sec=30, max_images=12, max_videos=4, max_audios=4),
        _video_card("seedance-2.5-pro-zh-kk-720", "Seedance 2.5 Pro(zh-kk)", "Seedance", "zongheng_video", "per_second", "24.0000", ["720P"], max_sec=30, max_images=16, max_videos=6, max_audios=6),
        _video_card("seedance-2.5-pro-zh-sd-480", "Seedance 2.5 Pro(zh-sd-480)", "Seedance", "zongheng_video", "per_second", "15.0000", ["480P"], max_sec=30, max_images=16),
        _video_card("seedance-2.5-pro-zh-sd-720", "Seedance 2.5 Pro(zh-sd-720)", "Seedance", "zongheng_video", "per_second", "26.0000", ["720P"], max_sec=30, max_images=20),
        _video_card("seedance-2.5-pro-zh-sd-call-720", "Seedance 2.5 Pro(zh-sd-按次720)", "Seedance", "zongheng_video", "per_call", "180.0000", ["720P"], max_sec=15, max_images=12),
        _video_card("seedance-2.5-pro-zh-tt-1080", "Seedance 2.5 Pro(zh-tt-1080)", "Seedance", "zongheng_video", "per_second", "42.0000", ["1080P"], max_sec=30, max_images=30, max_videos=10, max_audios=10),
        _video_card("seedance-2.5-pro-zh-tt-480", "Seedance 2.5 Pro(zh-tt-480)", "Seedance", "zongheng_video", "per_second", "16.0000", ["480P"], max_sec=30, max_images=20),
        _video_card("seedance-2.5-pro-zh-tt-720", "Seedance 2.5 Pro(zh-tt-720)", "Seedance", "zongheng_video", "per_second", "28.0000", ["720P"], max_sec=30, max_images=24, max_videos=8, max_audios=8),
        _video_card("seedance-2.5-pro-zh-allday-720", "Seedance 2.5 Pro(zh-全天720)", "Seedance", "zongheng_video", "per_second", "25.0000", ["720P"], max_sec=30),
        _video_card("seedance-2.5-pro-zh-promo-720", "Seedance 2.5 Pro(zh-特价720)", "Seedance", "zongheng_video", "per_second", "19.0000", ["720P"], max_sec=20),
        _video_card("seedance-2.5-pro-zh-stable-720", "Seedance 2.5 Pro(zh-稳定-720)", "Seedance", "zongheng_video", "per_second", "30.0000", ["720P"], max_sec=30),
        _video_card("seedance-2.5-pro-zh-stable-t480", "Seedance 2.5 Pro(zh-稳定-T480)", "Seedance", "zongheng_video", "per_second", "17.0000", ["480P"], max_sec=30),
        _video_card("seedance-2.5-pro-zh-stable-t720", "Seedance 2.5 Pro(zh-稳定-T720)", "Seedance", "zongheng_video", "per_second", "29.0000", ["720P"], max_sec=30),
        # Seedance 2.0 (12)
        _video_card("seedance-2.0-wf-v1-real", "Seedance 2.0(wf-v1-内置真人)", "Seedance", "zongheng_video", "per_call", "95.0000", ["480P"], max_sec=15),
        _video_card("seedance-2.0-wf-720-b", "Seedance 2.0(wf-720-B)", "Seedance", "zongheng_video", "per_call", "140.0000", ["720P"], max_sec=15),
        _video_card("seedance-2.0-wf-720-vertical", "Seedance 2.0(wf-720-竖屏)", "Seedance", "zongheng_video", "per_call", "135.0000", ["720P"], max_sec=15, ratios=["9:16"]),
        _video_card("seedance-2.0-wf-v-native-real", "Seedance 2.0(wf-v-原生真人)", "Seedance", "zongheng_video", "per_call", "150.0000", ["720P"], max_sec=15),
        _video_card("seedance-2.0-wf-per-sec", "Seedance 2.0(wf-按秒)", "Seedance", "zongheng_video", "per_second", "18.0000", ["480P", "720P"], max_sec=15),
        _video_card("seedance-2.0-zh-00", "Seedance 2.0(zh-00)", "Seedance", "zongheng_video", "per_call", "110.0000", ["480P", "720P"], max_sec=15),
        _video_card("seedance-2.0-zh-300s", "Seedance 2.0(zh-300s)", "Seedance", "zongheng_video", "per_call", "125.0000", ["720P"], max_sec=15),
        _video_card("seedance-2.0-zh-j-720", "Seedance 2.0(zh-j-720)", "Seedance", "zongheng_video", "per_second", "21.0000", ["720P"], max_sec=15),
        _video_card("seedance-2.0-zh-j-call-720", "Seedance 2.0(zh-j-按次720)", "Seedance", "zongheng_video", "per_call", "130.0000", ["720P"], max_sec=15),
        _video_card("seedance-2.0-zh-tt", "Seedance 2.0(zh-tt)", "Seedance", "zongheng_video", "per_call", "90.0000", ["480P"], max_sec=15),
        _video_card("seedance-2.0-zh-tt-720", "Seedance 2.0(zh-tt-720)", "Seedance", "zongheng_video", "per_call", "138.0000", ["720P"], max_sec=15),
        _video_card("seedance-2.0-zh-x-vertical", "Seedance 2.0(zh-x-竖屏)", "Seedance", "zongheng_video", "per_call", "120.0000", ["720P"], max_sec=15, ratios=["9:16"]),
        # Seedance 2.0 Fast (7)
        _video_card("seedance-2.0-fast-wf-v1-720p", "Seedance 2.0 Fast(wf-v1-720p)", "Seedance", "zongheng_video", "per_call", "88.0000", ["720P"], max_sec=10),
        _video_card("seedance-2.0-fast-wf-v1-real", "Seedance 2.0 Fast(wf-v1-内置过真人)", "Seedance", "zongheng_video", "per_call", "68.0000", ["480P"], max_sec=10),
        _video_card("seedance-2.0-fast-wf-720-b", "Seedance 2.0 Fast(wf-720-B)", "Seedance", "zongheng_video", "per_call", "85.0000", ["720P"], max_sec=10),
        _video_card("seedance-2.0-fast-wf-720-c", "Seedance 2.0 Fast(wf-720-C)", "Seedance", "zongheng_video", "per_call", "82.0000", ["720P"], max_sec=10),
        _video_card("seedance-2.0-fast-wf-c-i2v-480", "Seedance 2.0 Fast(wf-c-图生480)", "Seedance", "zongheng_video", "per_call", "55.0000", ["480P"], modes=["first_frame", "multimodal_ref"], max_sec=10),
        _video_card("seedance-2.0-fast-wf-c-t2v-480", "Seedance 2.0 Fast(wf-c-文生480)", "Seedance", "zongheng_video", "per_call", "52.0000", ["480P"], modes=["t2v"], max_sec=10),
        _video_card("seedance-2.0-fast-wf-c-fl-480", "Seedance 2.0 Fast(wf-c-首尾帧480)", "Seedance", "zongheng_video", "per_call", "62.0000", ["480P"], modes=["first_last_frame"], max_sec=10),
        # Alibaba Wan 3.0 (3)
        _video_card("wan3.0-video-480p", "wan3.0-video (480p)", "Alibaba Wan", "wan_video", "per_second", "12.0000", ["480P"], max_sec=15),
        _video_card("wan3.0-video-720p", "wan3.0-video (720p)", "Alibaba Wan", "wan_video", "per_second", "22.0000", ["720P"], max_sec=15),
        _video_card("wan3.0-video-1080p", "wan3.0-video (1080p)", "Alibaba Wan", "wan_video", "per_second", "36.0000", ["1080P"], max_sec=15),
        # Grok (2)
        _video_card("grok-zh-ttp-480p", "Grok(zh-ttp 480P)", "xAI Grok", "grok_video", "per_second", "16.0000", ["480P"], min_sec=5, max_sec=15, max_images=6, max_videos=0, max_audios=0),
        _video_card("grok-zh-ttp-720p", "Grok(zh-ttp 720P)", "xAI Grok", "grok_video", "per_second", "26.0000", ["720P"], min_sec=5, max_sec=15, max_images=6, max_videos=0, max_audios=0),
        # --- 4 Audio / Speech / ASR Models ---
        {
            "model_key": "speech-2.8-hd",
            "display_name": "speech-2.8-hd",
            "modality": "tts",
            "provider_family": "MiniMax",
            "protocol_profile": "minimax_tts",
            "billing_mode": "per_call",
            "unit_price_credits": "12.0000",
            "output_price_credits": None,
            "pricing_meta_json": {"unit": "积分/千字符", "first_voice_surcharge_credits": "1000.0000"},
            "capabilities_json": {
                "modes": ["tts", "voice_clone"],
                "emotions": ["auto", "happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"],
                "formats": ["mp3", "pcm", "flac", "wav", "pcmu_raw", "pcmu_wav", "opus"],
                "sample_rates": [8000, 16000, 22050, 24000, 32000, 44100, 48000],
                "channels": [1, 2],
                "mp3_bitrates": [32000, 64000, 128000, 256000],
                "voice_effects": ["spacious_echo", "auditorium_echo", "lofi_telephone", "robotic"],
                "language_boost_count": 39,
            },
        },
        {
            "model_key": "speech-2.8-turbo",
            "display_name": "speech-2.8-turbo",
            "modality": "tts",
            "provider_family": "MiniMax",
            "protocol_profile": "minimax_tts",
            "billing_mode": "per_call",
            "unit_price_credits": "8.0000",
            "output_price_credits": None,
            "pricing_meta_json": {"unit": "积分/千字符", "first_voice_surcharge_credits": "1000.0000"},
            "capabilities_json": {
                "modes": ["tts", "voice_clone"],
                "emotions": ["auto", "happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"],
                "formats": ["mp3", "pcm", "flac", "wav", "pcmu_raw", "pcmu_wav", "opus"],
                "sample_rates": [8000, 16000, 22050, 24000, 32000, 44100, 48000],
                "channels": [1, 2],
                "mp3_bitrates": [32000, 64000, 128000, 256000],
                "voice_effects": ["spacious_echo", "auditorium_echo", "lofi_telephone", "robotic"],
                "language_boost_count": 39,
            },
        },
        {
            "model_key": "voice-design-preview",
            "display_name": "音色设计试听价格载体",
            "modality": "tts",
            "provider_family": "MiniMax",
            "protocol_profile": "minimax_tts",
            "billing_mode": "per_call",
            "unit_price_credits": "20.0000",
            "output_price_credits": None,
            "pricing_meta_json": {"unit": "积分/次"},
            "capabilities_json": {"modes": ["voice_design"]},
        },
        {
            "model_key": "minimax-asr-1.0",
            "display_name": "MiniMax ASR 1.0",
            "modality": "asr",
            "provider_family": "MiniMax",
            "protocol_profile": "minimax_asr",
            "billing_mode": "per_second",
            "unit_price_credits": "0.5000",
            "output_price_credits": None,
            "pricing_meta_json": {"unit": "积分/秒"},
            "capabilities_json": {"modes": ["asr"]},
        },
        # --- 1 Suno Music Workbench Card (20 operations) ---
        {
            "model_key": "suno-music",
            "display_name": "Suno 音乐",
            "modality": "music",
            "provider_family": "Suno",
            "protocol_profile": "suno_music",
            "billing_mode": "per_operation",
            "unit_price_credits": "71.0000",
            "output_price_credits": None,
            "pricing_meta_json": {
                "unit": "积分/操作",
                "operations": SUNO_OPERATIONS_PRICING,
            },
            "capabilities_json": {
                "modes": ["song_bgm", "sfx", "reference", "mashup", "hum", "expand_style"],
                "versions": ["suno-v6", "suno-v6-wild", "suno-v6-mini"],
                "formats": ["mp3", "wav", "aac", "flac", "opus"],
                "operations": list(SUNO_OPERATIONS_PRICING.keys()),
            },
        },
    ]
    return cards


def ensure_rate_cards_seeded(db: Session) -> int:
    """Idempotently seed all 64 RateCards if missing. Returns count inserted."""
    existing_keys = set(db.scalars(select(RateCard.model_key)).all())
    seeds = build_seed_rate_cards()
    inserted = 0
    for idx, item in enumerate(seeds):
        if item["model_key"] in existing_keys:
            continue
        db.add(RateCard(
            id=new_prefixed_id("rc"),
            model_key=item["model_key"],
            display_name=item["display_name"],
            modality=item["modality"],
            provider_family=item["provider_family"],
            protocol_profile=item["protocol_profile"],
            billing_mode=item["billing_mode"],
            unit_price_credits=Decimal(item["unit_price_credits"]),
            output_price_credits=(
                Decimal(item["output_price_credits"]) if item["output_price_credits"] is not None else None
            ),
            pricing_meta_json=item["pricing_meta_json"],
            capabilities_json=item["capabilities_json"],
            enabled=True,
            sort_order=idx,
        ))
        inserted += 1
    if inserted:
        db.commit()
    return inserted
