"""
Subtitle translator using Ollama (local LLM).
Translates SRT subtitles from English to Simplified Chinese.
Also translates standalone text (titles, descriptions).
"""

import json
import logging
import re
import time

import pysubs2
import requests

logger = logging.getLogger("yt2bili.translator")


class SubtitleTranslator:
    def __init__(self, config: dict):
        self.ollama_host = config.get("ollama_host", "http://localhost:11434")
        self.model = config.get("model", "qwen2.5:7b")
        self.batch_size = config.get("batch_size", 30)
        self.temperature = config.get("temperature", 0.3)
        self.api_url = f"{self.ollama_host.rstrip('/')}/api/chat"

    def translate_file(self, input_path: str, output_path: str):
        """
        Translate an SRT/VTT/ASS subtitle file to Chinese.
        Preserves all timestamps. Only text content is translated.
        """
        logger.info(f"Loading subtitles from: {input_path}")
        subs = pysubs2.load(input_path, encoding="utf-8")

        # Clean auto-generated subtitle artifacts (duplicate/overlapping lines)
        subs = self._clean_auto_subs(subs)

        # Collect entries with text
        entries = []
        for i, line in enumerate(subs):
            text = line.text.replace("\\N", " ").replace("\n", " ").strip()
            if text:
                entries.append({"index": i, "text": text})

        if not entries:
            logger.warning("No translatable text found in subtitles")
            subs.save(output_path, encoding="utf-8")
            return

        logger.info(f"Translating {len(entries)} subtitle lines...")

        # Translate in batches with sliding context window
        prev_context = ""
        translated_count = 0

        for batch_start in range(0, len(entries), self.batch_size):
            batch = entries[batch_start : batch_start + self.batch_size]
            batch_num = (batch_start // self.batch_size) + 1
            total_batches = (len(entries) + self.batch_size - 1) // self.batch_size

            logger.info(f"  Batch {batch_num}/{total_batches} ({len(batch)} lines)...")

            # Retry logic for each batch
            translations = None
            for attempt in range(3):
                try:
                    translations = self._translate_batch(batch, prev_context)
                    break
                except Exception as e:
                    logger.warning(
                        f"  Batch {batch_num} attempt {attempt + 1} failed: {e}"
                    )
                    if attempt < 2:
                        time.sleep(5 * (attempt + 1))

            if translations is None:
                logger.error(f"  Batch {batch_num} failed after 3 attempts. Using original text.")
                translations = {i: entry["text"] for i, entry in enumerate(batch)}

            # Map translations back to subtitle lines
            for local_idx, entry in enumerate(batch):
                zh_text = translations.get(local_idx, entry["text"])
                sub_index = entry["index"]
                subs[sub_index].text = zh_text
                translated_count += 1

            # Update sliding context with last 3 translated lines
            context_lines = []
            for entry in batch[-3:]:
                local_idx = batch.index(entry)
                zh = translations.get(local_idx, "")
                context_lines.append(f"{entry['text']} → {zh}")
            prev_context = "\n".join(context_lines)

            # Brief pause between batches
            time.sleep(1)

        logger.info(f"Translation complete: {translated_count}/{len(entries)} lines")

        # Save as SRT
        subs.save(output_path, encoding="utf-8")
        logger.info(f"Saved translated subtitles to: {output_path}")

    def _translate_batch(self, batch: list[dict], prev_context: str) -> dict:
        """
        Send a batch of subtitle lines to Ollama for translation.
        Returns a dict mapping local index → translated Chinese text.

        CRITICAL: We NEVER send timestamps to the LLM.
        Only text content is sent, with integer IDs for alignment.
        """
        system_prompt = (
            "You are an expert subtitle translator specializing in English to "
            "Simplified Chinese (简体中文).\n"
            "Guidelines:\n"
            "1. Translate concisely and naturally, matching conversational Chinese "
            "rhythm. Avoid stiff translationese (欧化中文).\n"
            "2. Maintain STRICT 1-to-1 correspondence with the input list. "
            "Do NOT merge, split, add, or remove any lines.\n"
            "3. Return ONLY a valid JSON array matching this schema exactly:\n"
            '   [{"id": 0, "zh": "翻译内容"}, {"id": 1, "zh": "翻译内容"}, ...]\n'
            "4. The number of items in your output MUST equal the number of items "
            "in the input. Every ID must be present.\n"
            "5. Do NOT include any text outside the JSON array. No explanations, "
            "no markdown formatting, no code blocks."
        )

        # Build user prompt
        input_items = [{"id": i, "en": entry["text"]} for i, entry in enumerate(batch)]

        user_parts = []
        if prev_context:
            user_parts.append(
                f"Previous context (reference only, do NOT translate these):\n{prev_context}\n"
            )
        user_parts.append(
            "Translate these subtitles:\n" + json.dumps(input_items, ensure_ascii=False)
        )
        user_prompt = "\n".join(user_parts)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": self.temperature},
        }

        response = requests.post(self.api_url, json=payload, timeout=180)
        response.raise_for_status()

        data = response.json()
        content = data["message"]["content"].strip()

        # Parse the JSON response, handling potential markdown code blocks
        content = self._extract_json(content)
        result_list = json.loads(content)

        # Build id → zh mapping
        translations = {}
        for item in result_list:
            idx = item.get("id")
            zh = item.get("zh", "")
            if idx is not None:
                translations[idx] = zh

        # Validate: ensure all IDs are present
        expected_ids = set(range(len(batch)))
        actual_ids = set(translations.keys())
        missing = expected_ids - actual_ids

        if missing:
            logger.warning(
                f"Translation response missing IDs: {missing}. "
                f"Filling with original text."
            )
            for mid in missing:
                translations[mid] = batch[mid]["text"]

        return translations

    def translate_text(self, text: str, context: str = "") -> str:
        """
        Translate a standalone text string (title, description) to Chinese.
        """
        if not text or not text.strip():
            return text

        system_prompt = (
            "You are a professional translator. Translate the following English text "
            "to natural Simplified Chinese (简体中文). Return ONLY the translation, "
            "nothing else. Keep it concise and natural."
        )

        user_prompt = f"Context: {context}\n\nTranslate:\n{text}" if context else text

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": self.temperature},
        }

        try:
            response = requests.post(self.api_url, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()
            result = data["message"]["content"].strip()
            # Remove any surrounding quotes the LLM might add
            result = result.strip('"').strip("'").strip('"').strip('"')
            return result
        except Exception as e:
            logger.error(f"Text translation failed: {e}. Returning original.")
            return text

    def _extract_json(self, content: str) -> str:
        """Extract JSON from LLM response, handling markdown code blocks."""
        # Remove markdown code blocks if present
        if "```" in content:
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
            if match:
                return match.group(1).strip()

        # Try to find JSON array directly
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            return match.group(0)

        return content

    def _clean_auto_subs(self, subs: pysubs2.SSAFile) -> pysubs2.SSAFile:
        """
        Clean auto-generated subtitle artifacts:
        - Remove duplicate/overlapping lines with identical text
        - Merge very short consecutive lines with the same content
        """
        if not subs:
            return subs

        cleaned = pysubs2.SSAFile()
        cleaned.info = subs.info.copy()
        cleaned.styles = subs.styles.copy()

        prev_text = None
        for line in subs:
            text = line.text.replace("\\N", " ").replace("\n", " ").strip()
            if not text:
                continue
            # Skip exact duplicates of the previous line
            if text == prev_text:
                continue
            prev_text = text
            cleaned.append(line)

        removed = len(subs) - len(cleaned)
        if removed > 0:
            logger.info(f"Cleaned {removed} duplicate subtitle lines")

        return cleaned
