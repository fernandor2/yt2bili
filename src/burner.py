"""
Subtitle burner - embeds Chinese subtitles into video using FFmpeg.
Burns subtitles permanently into the video frames (hardsubs).

Supports BorderStyle=3 (opaque bounding box) to cleanly cover and replace
pre-existing burned-in subtitles from the source video, which is the standard
approach used by localization groups (字幕组) on Bilibili.
"""

import logging
import os
import subprocess

logger = logging.getLogger("yt2bili.burner")


class SubtitleBurner:
    def __init__(self, config: dict):
        self.font_name = config.get("font_name", "Noto Sans CJK SC")
        self.font_size = config.get("font_size", 20)
        self.preset = config.get("ffmpeg_preset", "faster")
        self.crf = config.get("ffmpeg_crf", 20)

        # Subtitle box styling:
        # border_style: 3 = Opaque background box (covers pre-existing burned-in text)
        # border_style: 1 = Standard outline + drop shadow (transparent background)
        self.border_style = config.get("border_style", 3)
        self.box_padding = config.get("box_padding", 4)
        self.margin_v = config.get("margin_v", 30)
        self.box_color = config.get("box_color", "&H00000000")    # 100% solid black box
        self.text_color = config.get("text_color", "&H00FFFFFF")  # Crisp white text

    def burn(self, video_path: str, subtitle_path: str, output_path: str):
        """
        Burn SRT subtitles into video using FFmpeg.
        Uses libx264 encoding with configurable CRF/preset.
        Audio stream is copied without re-encoding to preserve original audio quality.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video not found: {video_path}")
        if not os.path.exists(subtitle_path):
            raise FileNotFoundError(f"Subtitle not found: {subtitle_path}")

        # FFmpeg subtitle filter requires escaping special characters in file paths
        escaped_sub = subtitle_path.replace(":", r"\:").replace("'", r"\'")

        # Build ASS force_style string based on border_style
        if self.border_style == 3:
            # Option 2: Opaque bounding box covering pre-existing burned-in subtitles
            style = (
                f"FontName={self.font_name},"
                f"FontSize={self.font_size},"
                f"PrimaryColour={self.text_color},"
                f"OutlineColour={self.box_color},"
                f"BackColour={self.box_color},"
                f"BorderStyle=3,"
                f"Outline={self.box_padding},"
                f"Shadow=0,"
                f"MarginV={self.margin_v}"
            )
            logger.info("Using OPAQUE BACKGROUND BOX (BorderStyle=3) to cover pre-existing subtitles")
        else:
            # Standard outline with transparent background
            style = (
                f"FontName={self.font_name},"
                f"FontSize={self.font_size},"
                f"PrimaryColour={self.text_color},"
                f"OutlineColour=&H00000000,"
                f"BackColour=&H80000000,"
                f"BorderStyle=1,"
                f"Outline=2,"
                f"Shadow=1,"
                f"MarginV={self.margin_v}"
            )
            logger.info("Using standard text outline (BorderStyle=1)")

        vf_filter = f"subtitles='{escaped_sub}':force_style='{style}'"

        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output without prompting
            "-i", video_path,
            "-vf", vf_filter,
            "-c:v", "libx264",
            "-preset", self.preset,
            "-crf", str(self.crf),
            "-c:a", "copy",  # Keep original audio completely untouched
            "-movflags", "+faststart",  # Enable fast web streaming
            output_path,
        ]

        logger.info(f"FFmpeg command: {' '.join(cmd[:6])}... → {output_path}")
        logger.info(
            f"  Settings: preset={self.preset}, crf={self.crf}, "
            f"font={self.font_name} {self.font_size}pt, margin_v={self.margin_v}"
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=7200,  # 2 hour timeout for long-form videos
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
