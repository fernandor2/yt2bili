"""
Whisper client for standalone speech-to-text service.
Calls the OpenAI-compatible Whisper REST API running in the shared Docker network 'ai-net'
(e.g., http://whisper:8000/v1/audio/transcriptions).
Used as a high-accuracy fallback when YouTube videos do not contain captions.
"""

import logging
import os
import requests

logger = logging.getLogger("yt2bili.whisper")


class WhisperClient:
    def __init__(self, config: dict):
        whisper_cfg = config.get("whisper", {})
        self.enabled = whisper_cfg.get("enabled", True)
        self.url = whisper_cfg.get("url", "http://whisper:8000/v1/audio/transcriptions")
        self.model = whisper_cfg.get("model", "deepdml/faster-whisper-large-v3-turbo-ct2")
        self.timeout = whisper_cfg.get("timeout", 300)

    def transcribe(self, media_path: str, output_srt_path: str) -> str | None:
        """
        Send audio/video to the standalone Whisper service and save returned SRT subtitles.

        Args:
            media_path: Local filesystem path to the video/audio file.
            output_srt_path: Target path to save the returned .srt file.

        Returns:
            output_srt_path if transcription succeeded, None otherwise.
        """
        if not self.enabled:
            logger.info("Whisper fallback transcription is disabled.")
            return None

        if not os.path.exists(media_path):
            logger.error(f"Media file not found for Whisper transcription: {media_path}")
            return None

        filename = os.path.basename(media_path)
        logger.info(f"Sending '{filename}' to Whisper service ({self.url})...")

        try:
            with open(media_path, "rb") as f:
                files = {
                    "file": (filename, f, "video/mp4"),
                }
                data = {
                    "model": self.model,
                    "response_format": "srt",
                }

                response = requests.post(
                    self.url,
                    files=files,
                    data=data,
                    timeout=self.timeout,
                )

            if response.status_code == 200:
                srt_content = response.text.strip()
                if not srt_content:
                    logger.warning("Whisper service responded 200 OK but returned empty subtitles.")
                    return None

                with open(output_srt_path, "w", encoding="utf-8") as out:
                    out.write(srt_content)

                logger.info(f"✅ Whisper transcription successful. Saved to: {output_srt_path}")
                return output_srt_path
            else:
                logger.error(
                    f"Whisper service returned HTTP {response.status_code}: {response.text[:300]}"
                )
                return None

        except requests.exceptions.ConnectionError:
            logger.warning(
                f"Could not connect to Whisper service at {self.url}. "
                f"Is the 'whisper' container running on the 'ai-net' network?"
            )
            return None
        except requests.exceptions.Timeout:
            logger.error(f"Whisper transcription timed out after {self.timeout}s.")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error during Whisper transcription: {e}")
            return None
