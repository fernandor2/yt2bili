"""
Subtitle burner - embeds Chinese subtitles into video using FFmpeg.
Burns subtitles permanently into the video frames (hardsubs).

This is the recommended approach for Bilibili because:
1. CC subtitles are disabled by default for viewers
2. Burned subs remain readable over scrolling danmaku (弹幕)
3. Full font/style control (Noto CJK with outline)
"""

import logging
import os
import subprocess

logger = logging.getLogger("yt2bili.burner")


class SubtitleBurner:
    def __init__(self, config: dict):
        self.font_name = config.get("font_name", "Noto Sans CJK SC")
        self.font_size = config.get("font_size", 18)
        self.preset = config.get("ffmpeg_preset", "faster")
        self.crf = config.get("ffmpeg_crf", 20)

    def burn(self, video_path: str, subtitle_path: str, output_path: str):
        """
        Burn SRT subtitles into video using FFmpeg.
        Uses libx264 encoding with configurable CRF/preset.
        Audio stream is copied without re-encoding.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video not found: {video_path}")
        if not os.path.exists(subtitle_path):
            raise FileNotFoundError(f"Subtitle not found: {subtitle_path}")

        # FFmpeg subtitle filter requires escaping special chars in path
        # On Linux/Docker, paths are straightforward
        escaped_sub = subtitle_path.replace(":", r"\:").replace("'", r"\'")

        # Build the subtitles filter with Chinese-friendly styling
        # PrimaryColour format: &HAABBGGRR (ASS format, note: BGR not RGB)
        # White text = &H00FFFFFF
        # Black outline = &H00000000
        style = (
            f"FontName={self.font_name},"
            f"FontSize={self.font_size},"
            f"PrimaryColour=&H00FFFFFF,"
            f"OutlineColour=&H00000000,"
            f"BackColour=&H80000000,"
            f"BorderStyle=1,"
            f"Outline=2,"
            f"Shadow=1,"
            f"MarginV=30"
        )

        vf_filter = f"subtitles='{escaped_sub}':force_style='{style}'"

        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output
            "-i", video_path,
            "-vf", vf_filter,
            "-c:v", "libx264",
            "-preset", self.preset,
            "-crf", str(self.crf),
            "-c:a", "copy",  # Keep original audio untouched
            "-movflags", "+faststart",  # Enable streaming
            output_path,
        ]

        logger.info(f"FFmpeg command: {' '.join(cmd[:6])}... → {output_path}")
        logger.info(
            f"  Settings: preset={self.preset}, crf={self.crf}, "
            f"font={self.font_name} {self.font_size}pt"
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=7200,  # 2 hour timeout for long videos
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg stderr:\n{result.stderr[-2000:]}")
                raise RuntimeError(
                    f"FFmpeg exited with code {result.returncode}"
                )

            # Verify output file exists and has reasonable size
            if not os.path.exists(output_path):
                raise RuntimeError("FFmpeg output file not created")

            output_size = os.path.getsize(output_path)
            input_size = os.path.getsize(video_path)

            if output_size < input_size * 0.1:
                logger.warning(
                    f"Output file suspiciously small "
                    f"({output_size/1e6:.1f}MB vs {input_size/1e6:.1f}MB input). "
                    f"Possible encoding error."
                )

            logger.info(
                f"Burn-in complete: {output_size/1e6:.1f}MB "
                f"(input: {input_size/1e6:.1f}MB)"
            )

        except subprocess.TimeoutExpired:
            logger.error("FFmpeg timed out after 2 hours")
            raise
        except Exception as e:
            logger.exception(f"Subtitle burn-in failed: {e}")
            raise
