"""6 independent vendor protocol adapters supporting sync, async polling & Webhook callbacks.

Adapters:
1. OpenAIImageAdapter   : openlux_gpt_images, qwen_images, gemini_images
2. ArkImageAdapter      : ark_images (Volcengine Seedream v3)
3. MidjourneyAdapter    : midjourney_proxy (imagine + U/V/upscale/zoom/pan actions)
4. VideoAsyncAdapter    : zongheng_video (Seedance), wan_video, grok_video
5. MiniMaxMediaAdapter  : minimax_video, minimax_tts (t2a_v2), minimax_asr
6. SunoMusicAdapter     : suno_music (v6 / wild / mini, 6 modes & 20 operations)
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.config import get_settings


@dataclass
class AdapterCallResult:
    mock: bool
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    pending_external_task_id: Optional[str] = None


def _decode_b64_or_fetch(item: dict[str, Any], default_mime: str, default_name: str) -> dict[str, Any]:
    if item.get("b64_json") or item.get("b64_data"):
        raw = base64.b64decode(item.get("b64_json") or item.get("b64_data"))
    elif item.get("audio_hex"):
        raw = bytes.fromhex(item["audio_hex"])
    elif item.get("url"):
        with httpx.Client(timeout=60) as client:
            r = client.get(item["url"])
            r.raise_for_status()
            raw = r.content
    else:
        raw = b""
    return {
        "filename": item.get("filename") or default_name,
        "content_type": item.get("content_type") or default_mime,
        "bytes": raw,
        "meta": {k: v for k, v in item.items() if k not in ("b64_json", "b64_data", "audio_hex")},
    }


class BaseProviderAdapter:
    provider_family: str = "generic"

    def build_request(
        self,
        *,
        base_url: str,
        model_id: str,
        mode: str,
        prompt: str,
        params: dict[str, Any],
        callback_url: Optional[str] = None,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError

    def parse_response(
        self,
        data: dict[str, Any],
        *,
        mode: str,
        params: dict[str, Any],
    ) -> AdapterCallResult:
        raise NotImplementedError

    def invoke(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        mode: str,
        prompt: str,
        params: dict[str, Any],
        callback_url: Optional[str] = None,
    ) -> AdapterCallResult:
        url, payload = self.build_request(
            base_url=base_url.rstrip("/"),
            model_id=model_id,
            mode=mode,
            prompt=prompt,
            params=params,
            callback_url=callback_url,
        )
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"{self.provider_family} returned HTTP {resp.status_code}: {resp.text[:240]}")
            data = resp.json()
        return self.parse_response(data, mode=mode, params=params)


class OpenAIImageAdapter(BaseProviderAdapter):
    provider_family = "openai_images"

    def build_request(self, *, base_url, model_id, mode, prompt, params, callback_url=None):
        payload: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "n": max(1, int(params.get("count", 1))),
            "response_format": "b64_json",
        }
        if params.get("size") and params["size"] != "auto":
            payload["size"] = params["size"]
        if params.get("quality") and params["quality"] != "auto":
            payload["quality"] = params["quality"]
        if params.get("background") and params["background"] != "auto":
            payload["background"] = params["background"]
        if params.get("output_format"):
            payload["output_format"] = params["output_format"]
        return f"{base_url}/v1/images/generations", payload

    def parse_response(self, data, *, mode, params):
        fmt = str(params.get("output_format") or "png").lower()
        mime = f"image/{'jpeg' if fmt == 'jpeg' else fmt}"
        items = data.get("data") or []
        artifacts = [
            _decode_b64_or_fetch(it, mime, f"openai_img_{idx + 1}.{fmt}")
            for idx, it in enumerate(items)
        ]
        return AdapterCallResult(mock=False, artifacts=artifacts, metadata={"provider": self.provider_family})


class ArkImageAdapter(BaseProviderAdapter):
    provider_family = "ark"

    def build_request(self, *, base_url, model_id, mode, prompt, params, callback_url=None):
        payload = {
            "model": model_id,
            "prompt": prompt,
            "size": params.get("size") if params.get("size") not in (None, "auto") else "1024x1024",
            "response_format": "b64_json",
            "watermark": False,
        }
        return f"{base_url}/api/v3/images/generations", payload

    def parse_response(self, data, *, mode, params):
        fmt = str(params.get("output_format") or "png").lower()
        mime = f"image/{'jpeg' if fmt == 'jpeg' else fmt}"
        items = data.get("data") or []
        artifacts = [
            _decode_b64_or_fetch(it, mime, f"ark_seedream_{idx + 1}.{fmt}")
            for idx, it in enumerate(items)
        ]
        return AdapterCallResult(mock=False, artifacts=artifacts, metadata={"provider": self.provider_family})


class MidjourneyAdapter(BaseProviderAdapter):
    provider_family = "midjourney"

    def build_request(self, *, base_url, model_id, mode, prompt, params, callback_url=None):
        action = str(params.get("mj_action") or mode or "imagine").lower()
        if action != "imagine" and params.get("parent_external_task_id"):
            payload: dict[str, Any] = {
                "taskId": params["parent_external_task_id"],
                "action": action.upper(),
            }
            if callback_url:
                payload["notifyHook"] = callback_url
            return f"{base_url}/mj/submit/action", payload

        full_prompt = prompt
        if params.get("stylize") is not None and "--stylize" not in full_prompt:
            full_prompt += f" --stylize {int(params['stylize'])}"
        if params.get("seed") is not None and "--seed" not in full_prompt:
            full_prompt += f" --seed {int(params['seed'])}"
        if params.get("aspect_ratio") and "--ar" not in full_prompt:
            full_prompt += f" --ar {params['aspect_ratio']}"
        payload = {
            "botType": "NIJI_JOURNEY" if "niji" in model_id.lower() else "MID_JOURNEY",
            "prompt": full_prompt.strip(),
            "mode": params.get("prompt_mode") or "visual",
        }
        if callback_url:
            payload["notifyHook"] = callback_url
        return f"{base_url}/mj/submit/imagine", payload

    def parse_response(self, data, *, mode, params):
        if data.get("b64_data") or data.get("imageUrl"):
            art = _decode_b64_or_fetch(
                {"b64_data": data.get("b64_data"), "url": data.get("imageUrl")},
                "image/png",
                "mj_output.png",
            )
            return AdapterCallResult(mock=False, artifacts=[art], metadata={"provider": self.provider_family, "raw": data})
        ext_id = str(data.get("result") or data.get("task_id") or data.get("id") or "")
        if ext_id:
            return AdapterCallResult(
                mock=False,
                artifacts=[],
                metadata={"provider": self.provider_family, "submitted": data},
                pending_external_task_id=ext_id,
            )
        raise RuntimeError(f"midjourney returned unexpected response: {data}")


class VideoAsyncAdapter(BaseProviderAdapter):
    provider_family = "seedance_video"

    def build_request(self, *, base_url, model_id, mode, prompt, params, callback_url=None):
        payload: dict[str, Any] = {
            "model": model_id,
            "mode": mode,
            "prompt": prompt,
            "resolution": params.get("resolution") or "720P",
            "aspect_ratio": params.get("aspect_ratio") or "16:9",
            "duration_seconds": int(params.get("duration_seconds", 5)),
        }
        for key in ("first_frame_object_id", "last_frame_object_id", "ref_image_ids", "ref_video_ids", "ref_audio_ids", "draft_job_id"):
            if params.get(key):
                payload[key] = params[key]
        if callback_url:
            payload["callback_url"] = callback_url
        return f"{base_url}/v1/video/generations", payload

    def parse_response(self, data, *, mode, params):
        if data.get("b64_data") or data.get("video_url"):
            art = _decode_b64_or_fetch(
                {"b64_data": data.get("b64_data"), "url": data.get("video_url")},
                "video/mp4",
                "video_output.mp4",
            )
            return AdapterCallResult(mock=False, artifacts=[art], metadata={"provider": self.provider_family})
        ext_id = str(data.get("task_id") or data.get("id") or "")
        if ext_id:
            return AdapterCallResult(
                mock=False,
                artifacts=[],
                metadata={"provider": self.provider_family, "submitted": data},
                pending_external_task_id=ext_id,
            )
        raise RuntimeError(f"video provider returned unexpected response: {data}")


class MiniMaxMediaAdapter(BaseProviderAdapter):
    provider_family = "minimax"

    def build_request(self, *, base_url, model_id, mode, prompt, params, callback_url=None):
        if mode == "asr":
            payload = {
                "model": model_id,
                "source_audio_object_id": params.get("source_audio_object_id"),
                "language": params.get("language_boost", "auto"),
            }
            return f"{base_url}/v1/audio/transcriptions", payload

        if mode in ("tts", "voice_clone", "voice_design"):
            payload = {
                "model": model_id,
                "mode": mode,
                "text": prompt,
                "voice_setting": {
                    "voice_id": params.get("voice_id") or "default_voice",
                    "speed": float(params.get("speed", 1.0)),
                    "vol": float(params.get("volume", 1.0)),
                    "pitch": int(params.get("pitch", 0)),
                    "emotion": params.get("emotion", "auto"),
                },
                "audio_setting": {
                    "format": params.get("output_format", "mp3"),
                    "sample_rate": int(params.get("sample_rate", 32000)),
                    "bitrate": int(params.get("mp3_bitrate", 128000)),
                    "channel": int(params.get("channels", 1)),
                },
                "language_boost": params.get("language_boost", "auto"),
                "subtitle_enable": bool(params.get("generate_subtitles", False)),
                "watermark": bool(params.get("watermark", False)),
            }
            if params.get("pronunciation_dict"):
                payload["pronunciation_dict"] = params["pronunciation_dict"]
            if params.get("voice_effect"):
                payload["voice_modify"] = {"sound_effects": params["voice_effect"]}
            return f"{base_url}/v1/t2a_v2", payload

        # MiniMax H3 video
        payload = {
            "model": model_id,
            "mode": mode,
            "prompt": prompt,
            "resolution": params.get("resolution") or "768P",
            "duration": int(params.get("duration_seconds", 6)),
        }
        if callback_url:
            payload["callback_url"] = callback_url
        return f"{base_url}/v1/video_generation", payload

    def parse_response(self, data, *, mode, params):
        if mode == "asr":
            return AdapterCallResult(
                mock=False,
                artifacts=[],
                metadata={"provider": self.provider_family, "transcript": data.get("text") or data.get("transcript", "")},
            )
        if mode in ("tts", "voice_clone", "voice_design"):
            audio_data = data.get("data") if isinstance(data.get("data"), dict) else data
            fmt = str(params.get("output_format") or "mp3").lower()
            mime = "audio/wav" if fmt == "wav" else "audio/mpeg"
            art = _decode_b64_or_fetch(
                {
                    "audio_hex": audio_data.get("audio"),
                    "b64_data": audio_data.get("b64_data"),
                    "url": audio_data.get("audio_url"),
                },
                mime,
                f"minimax_tts.{fmt}",
            )
            return AdapterCallResult(
                mock=False,
                artifacts=[art],
                metadata={"provider": self.provider_family, "subtitles": audio_data.get("subtitles")},
            )
        ext_id = str(data.get("task_id") or data.get("id") or "")
        if ext_id:
            return AdapterCallResult(
                mock=False,
                artifacts=[],
                metadata={"provider": self.provider_family, "submitted": data},
                pending_external_task_id=ext_id,
            )
        raise RuntimeError(f"minimax returned unexpected response: {data}")


class SunoMusicAdapter(BaseProviderAdapter):
    provider_family = "suno"

    def build_request(self, *, base_url, model_id, mode, prompt, params, callback_url=None):
        op = str(params.get("operation") or mode or "song_bgm").lower()
        payload: dict[str, Any] = {
            "mv": params.get("version") or "suno-v6",
            "operation": op,
            "mode": mode,
            "make_instrumental": bool(params.get("instrumental", False)),
            "output_format": params.get("output_format") or "mp3",
        }
        if params.get("lyrics"):
            payload["prompt"] = params["lyrics"]
        else:
            payload["gpt_description_prompt"] = prompt
        if params.get("style"):
            payload["tags"] = params["style"]
        if params.get("title"):
            payload["title"] = params["title"]
        if callback_url:
            payload["callback_url"] = callback_url
        return f"{base_url}/v1/suno/generate", payload

    def parse_response(self, data, *, mode, params):
        if data.get("b64_data") or data.get("audio_url"):
            fmt = str(params.get("output_format") or "mp3").lower()
            mime = "audio/wav" if fmt == "wav" else "audio/mpeg"
            art = _decode_b64_or_fetch(
                {"b64_data": data.get("b64_data"), "url": data.get("audio_url")},
                mime,
                f"suno_output.{fmt}",
            )
            return AdapterCallResult(mock=False, artifacts=[art], metadata={"provider": self.provider_family})
        ext_id = str(data.get("task_id") or data.get("id") or "")
        if ext_id:
            return AdapterCallResult(
                mock=False,
                artifacts=[],
                metadata={"provider": self.provider_family, "submitted": data},
                pending_external_task_id=ext_id,
            )
        return AdapterCallResult(mock=False, artifacts=[], metadata={"provider": self.provider_family, "result": data})


_ADAPTER_BY_PROFILE: dict[str, BaseProviderAdapter] = {
    "openlux_gpt_images": OpenAIImageAdapter(),
    "qwen_images": OpenAIImageAdapter(),
    "gemini_images": OpenAIImageAdapter(),
    "ark_images": ArkImageAdapter(),
    "midjourney_proxy": MidjourneyAdapter(),
    "zongheng_video": VideoAsyncAdapter(),
    "wan_video": VideoAsyncAdapter(),
    "grok_video": VideoAsyncAdapter(),
    "minimax_video": MiniMaxMediaAdapter(),
    "minimax_tts": MiniMaxMediaAdapter(),
    "minimax_asr": MiniMaxMediaAdapter(),
    "suno_music": SunoMusicAdapter(),
}


def get_adapter_for_profile(protocol_profile: str) -> BaseProviderAdapter:
    return _ADAPTER_BY_PROFILE.get(protocol_profile, OpenAIImageAdapter())


def resolve_env_credentials(protocol_profile: str) -> Optional[tuple[str, str]]:
    """Return (base_url, api_key) from vendor-specific env vars or unified MODEL_GATEWAY, else None."""
    s = get_settings()
    vendor_map: dict[str, tuple[str, str]] = {
        "openlux_gpt_images": (s.openai_image_base_url, s.openai_image_api_key),
        "qwen_images": (s.openai_image_base_url, s.openai_image_api_key),
        "gemini_images": (s.openai_image_base_url, s.openai_image_api_key),
        "ark_images": (s.ark_base_url, s.ark_api_key),
        "midjourney_proxy": (s.mj_base_url, s.mj_api_key),
        "zongheng_video": (s.seedance_base_url, s.seedance_api_key),
        "wan_video": (s.seedance_base_url, s.seedance_api_key),
        "grok_video": (s.seedance_base_url, s.seedance_api_key),
        "minimax_video": (s.minimax_base_url, s.minimax_api_key),
        "minimax_tts": (s.minimax_base_url, s.minimax_api_key),
        "minimax_asr": (s.minimax_base_url, s.minimax_api_key),
        "suno_music": (s.suno_base_url, s.suno_api_key),
    }
    base, key = vendor_map.get(protocol_profile, ("", ""))
    if base and key:
        return base, key
    if s.model_gateway_base_url and s.model_gateway_api_key:
        return s.model_gateway_base_url, s.model_gateway_api_key
    return None
