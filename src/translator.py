"""
Subtitle translator using Ollama (local LLM).
Translates SRT/VTT subtitles from Spanish/English to Simplified Chinese.
Also translates standalone text (titles, descriptions).
"""

import json
import logging
import re
import time

import pysubs2
import requests

logger = logging.getLogger("yt2bili.translator")


def clean_subtitle_text(raw: str) -> str:
    """
    Remove all karaoke timestamp tags (<00:00:03.080>), HTML/VTT formatting (<c>, </c>, <font...>),
    and newlines from a subtitle string, returning normalized single-line text.
    """
    if not raw:
        return ""
    # Strip all XML/HTML/VTT tags like <00:00:03.080>, <c>, </c>, <font color="...">, etc.
    t = re.sub(r"<[^>]+>", "", raw)
    # Replace ASS newlines (\\N) and regular newlines with space
    t = t.replace("\\N", " ").replace("\n", " ").replace("\r", " ")
    # Collapse multiple whitespace into a single space
    t = re.sub(r"\s+", " ", t).strip()
    return t


class SubtitleTranslator:
    def __init__(self, config: dict):
        self.ollama_host = config.get("ollama_host", "http://localhost:11434")
        self.model = config.get("model", "qwen2.5:14b")
        self.batch_size = config.get("batch_size", 15)
        self.temperature = config.get("temperature", 0.3)
        self.api_url = f"{self.ollama_host.rstrip('/')}/api/chat"

    def translate_file(self, input_path: str, output_path: str):
        """
        Translate an SRT/VTT/ASS subtitle file to Chinese.
        Cleans karaoke tags and overlapping auto-sub artifacts,
        translates text via Ollama, and saves clean SRT.
        """
        logger.info(f"Loading subtitles from: {input_path}")
        subs = pysubs2.load(input_path, encoding="utf-8")

        # Clean auto-generated subtitle artifacts (karaoke tags, duplicate/overlapping rolling lines)
        subs = self._clean_auto_subs(subs)

        # Collect translatable entries
        entries = []
        for i, line in enumerate(subs):
            cleaned = clean_subtitle_text(line.text)
            if cleaned:
                entries.append({"index": i, "text": cleaned})

        if not entries:
            logger.warning("No translatable text found in subtitles")
            subs.save(output_path, encoding="utf-8")
            return

        logger.info(f"Translating {len(entries)} clean subtitle lines (batch size: {self.batch_size})...")

        # Translate in batches with sliding context window
        prev_context = ""
        translated_count = 0

        for batch_start in range(0, len(entries), self.batch_size):
            batch = entries[batch_start : batch_start + self.batch_size]
            batch_num = (batch_start // self.batch_size) + 1
            total_batches = (len(entries) + self.batch_size - 1) // self.batch_size

            logger.info(f"  Batch {batch_num}/{total_batches} ({len(batch)} lines)...")

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
                        time.sleep(3 * (attempt + 1))

            if translations is None:
                logger.error(f"  Batch {batch_num} failed after 3 attempts. Falling back to cleaned original text.")
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
                if zh:
                    context_lines.append(f"{entry['text']} → {zh}")
            prev_context = "\n".join(context_lines)

            # Brief pause between batches to yield CPU
            time.sleep(1)

        logger.info(f"Translation complete: {translated_count}/{len(entries)} lines")

        # Save as clean SRT
        subs.save(output_path, encoding="utf-8")
        logger.info(f"Saved translated subtitles to: {output_path}")

    def _call_ollama_raw(self, items: list[dict], prev_context: str = "") -> dict[int, str]:
        """
        Core non-recursive call to Ollama.
        Sends a JSON object { "0": "text", "1": "text" } and parses the response into { 0: "zh", 1: "zh" }.
        """
        system_prompt = (
            "You are an expert subtitle translator specializing in translating Spanish and English to "
            "natural Simplified Chinese (简体中文).\n"
            "Guidelines:\n"
            "1. Translate concisely and naturally for video subtitles. Avoid stiff translationese.\n"
            "2. Return ONLY a valid JSON object where each key is the line ID string and value is the Chinese translation.\n"
            "   Example format:\n"
            '   {"0": "第一行翻译", "1": "第二行翻译"}\n'
            "3. You MUST include every input ID in the output object.\n"
            "4. Do NOT include explanations, markdown headers, or text outside the JSON object."
        )

        input_dict = {str(i): item["text"] for i, item in enumerate(items)}
        user_parts = []
        if prev_context:
            user_parts.append(f"Context from previous lines (reference only):\n{prev_context}\n")
        user_parts.append("Translate these lines:\n" + json.dumps(input_dict, ensure_ascii=False, indent=2))
        user_prompt = "\n".join(user_parts)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "num_ctx": 4096,
                "num_predict": 2048,
            },
        }

        # 10-minute timeout accommodates 14B model running on CPU
        response = requests.post(self.api_url, json=payload, timeout=600)
        response.raise_for_status()

        data = response.json()
        content = data["message"]["content"].strip()

        return self._parse_json_or_fallback(content, len(items))

    def _translate_batch(self, batch: list[dict], prev_context: str) -> dict[int, str]:
        """
        Translate a batch of subtitle lines with non-recursive missing ID resolution.
        """
        translations = self._call_ollama_raw(batch, prev_context)

        # Check for missing IDs
        expected_ids = set(range(len(batch)))
        actual_ids = set(translations.keys())
        missing = expected_ids - actual_ids

        if missing:
            logger.warning(
                f"Translation response missing IDs {sorted(missing)}. Retrying missing lines once..."
            )
            missing_items = [{"text": batch[m]["text"]} for m in sorted(missing)]
            try:
                retry_map = self._call_ollama_raw(missing_items, prev_context)
                for new_idx, orig_idx in enumerate(sorted(missing)):
                    if new_idx in retry_map and retry_map[new_idx].strip():
                        translations[orig_idx] = retry_map[new_idx]
            except Exception as e:
                logger.warning(f"Retry for missing lines failed ({e}).")

        # For any lines still missing, fill with cleaned original text (NEVER leave empty)
        still_missing = expected_ids - set(translations.keys())
        for m in still_missing:
            logger.warning(f"Line {m} could not be translated by LLM; using original text.")
            translations[m] = batch[m]["text"]

        return translations

    def _parse_json_or_fallback(self, content: str, expected_count: int) -> dict[int, str]:
        """
        Extract translations from LLM output using 3 layers of parsing:
        Layer 1: Strict JSON decode (handles {"0": "...", "1": "..."} or [{"id": 0, "zh": "..."}])
        Layer 2: Regex key-value pattern for malformed JSON strings
        Layer 3: Line-by-line numbered pattern ("0: 翻译", "1: 翻译")
        """
        translations: dict[int, str] = {}

        # Layer 1: JSON extraction
        clean_json_str = self._extract_json(content)
        try:
            parsed = json.loads(clean_json_str)
            if isinstance(parsed, dict):
                # Check for standard key-value map: {"0": "...", "1": "..."}
                for k, v in parsed.items():
                    try:
                        idx = int(k)
                        translations[idx] = str(v).strip()
                    except (ValueError, TypeError):
                        pass
                # Check if nested list inside dict, e.g. {"translations": [...]}
                if not translations:
                    for key in ["translations", "subtitles", "data", "result", "items"]:
                        if key in parsed and isinstance(parsed[key], list):
                            parsed = parsed[key]
                            break
            if isinstance(parsed, list):
                for i, item in enumerate(parsed):
                    if isinstance(item, dict):
                        idx = item.get("id", i)
                        zh = item.get("zh", item.get("text", item.get("translation", "")))
                        try:
                            translations[int(idx)] = str(zh).strip()
                        except (ValueError, TypeError):
                            translations[i] = str(zh).strip()
                    elif isinstance(item, str):
                        translations[i] = item.strip()
        except Exception:
            pass

        # Layer 2: If JSON failed or missed lines, extract via regex key-value
        if len(translations) < expected_count:
            kv_matches = re.findall(r'["\']?(\d+)["\']?\s*:\s*["\']([^"\'\n\r]+)["\']?', content)
            for idx_str, text in kv_matches:
                try:
                    idx = int(idx_str)
                    if idx not in translations and idx < expected_count:
                        translations[idx] = text.strip()
                except ValueError:
                    pass

        # Layer 3: Line-by-line numbered patterns: 0: xxx or 0. xxx
        if len(translations) < expected_count:
            line_matches = re.findall(r'(?:^|\n)\s*(\d+)[\.\:：\s]+([^\n\r]+)', content)
            for idx_str, text in line_matches:
                try:
                    idx = int(idx_str)
                    if idx not in translations and idx < expected_count:
                        translations[idx] = text.strip()
                except ValueError:
                    pass

        return translations

    def _extract_json(self, content: str) -> str:
        """Extract JSON from LLM response, stripping markdown wrappers."""
        if "```" in content:
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
            if match:
                content = match.group(1).strip()

        first_brace = content.find("{")
        last_brace = content.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            return content[first_brace : last_brace + 1]

        first_bracket = content.find("[")
        last_bracket = content.rfind("]")
        if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
            return content[first_bracket : last_bracket + 1]

        return content

    def _clean_auto_subs(self, subs: pysubs2.SSAFile) -> pysubs2.SSAFile:
        """
        Clean auto-generated subtitle artifacts:
        - Strip all karaoke timestamp tags (<00:00:03.080>) and HTML/styling tags
        - Merge speech-recognition rolling fragments into coherent sentences
        - Eliminate time overlaps so libass never stacks cues vertically on screen
        - Filter out empty or duplicate lines
        """
        if not subs:
            return subs

        raw_count = len(subs)
        cleaned_events = []

        for line in subs:
            text = clean_subtitle_text(line.text)
            if not text:
                continue

            ev = pysubs2.SSAEvent(
                start=line.start,
                end=line.end,
                text=text,
                style=line.style
            )

            if not cleaned_events:
                cleaned_events.append(ev)
                continue

            prev = cleaned_events[-1]

            # 1. Exact duplicate text -> extend previous duration
            if ev.text == prev.text:
                prev.end = max(prev.end, ev.end)
                continue

            # 2. Current cue extends previous cue (speech recognition progressive output)
            # e.g. prev: "Guido ya sabía", ev: "Guido ya sabía que McQueen iba a terminar así"
            if ev.text.startswith(prev.text) and (ev.start - prev.start < 4000):
                prev.text = ev.text
                prev.end = max(prev.end, ev.end)
                continue

            # 3. Previous cue already contains current cue -> absorb duration
            if prev.text.startswith(ev.text) and (ev.start - prev.start < 4000):
                prev.end = max(prev.end, ev.end)
                continue

            # 4. Remove word-level overlap if current cue repeats end of previous cue
            prev_words = prev.text.split()
            ev_words = ev.text.split()
            overlap_found = False
            for k in range(min(len(prev_words), len(ev_words)), 0, -1):
                if prev_words[-k:] == ev_words[:k]:
                    remaining = ev_words[k:]
                    if remaining:
                        ev.text = " ".join(remaining)
                    else:
                        prev.end = max(prev.end, ev.end)
                        overlap_found = True
                    break

            if overlap_found and not ev.text:
                continue

            # 5. Prevent libass vertical stacking: ensure previous cue ends before current starts
            if ev.start < prev.end:
                if ev.start > prev.start:
                    prev.end = ev.start
                else:
                    ev.start = prev.end

            # Ensure minimum duration of 500ms
            if ev.end <= ev.start:
                ev.end = ev.start + 1000

            cleaned_events.append(ev)

        cleaned = pysubs2.SSAFile()
        cleaned.info = subs.info.copy()
        cleaned.styles = subs.styles.copy()
        cleaned.events = cleaned_events

        logger.info(f"Cleaned auto-subs: {raw_count} raw cues → {len(cleaned)} clean cues")
        return cleaned

    def translate_text(self, text: str, context: str = "") -> str:
        """
        Translate a standalone text string (title, description) to Chinese.
        """
        if not text or not text.strip():
            return text

        # Guard: If text is a raw YouTube ID without spaces, do not attempt to translate it
        if re.match(r"^[a-zA-Z0-9_-]{8,15}$", text.strip()):
            logger.warning(f"translate_text received raw ID '{text}' instead of text. Skipping translation.")
            return text

        system_prompt = (
            "You are a professional translator. Translate the given text (Spanish or English) "
            "into natural Simplified Chinese (简体中文).\n"
            "Rules:\n"
            "1. Output ONLY the direct Chinese translation, without explanations, quotes, or notes.\n"
            "2. Never output meta-commentary, explanations, or notes about the input."
        )

        user_prompt = f"Translate this {context} to Simplified Chinese:\n{text}" if context else text

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
            response = requests.post(self.api_url, json=payload, timeout=300)
            response.raise_for_status()
            data = response.json()
            result = data["message"]["content"].strip()
            # Remove any surrounding quotes the LLM might add
            result = result.strip('"').strip("'").strip('“').strip('”')

            # Filter out meta-explanations from LLM if it refused to translate
            meta_phrases = ["上下文标识符", "没有实际文字内容", "无法翻译", "作为人工智能", "as an ai", "i cannot translate"]
            if any(phrase in result.lower() for phrase in meta_phrases):
                logger.warning(f"LLM returned meta-explanation instead of translation ('{result}'). Falling back to original.")
                return text

            return result
        except Exception as e:
            logger.error(f"Text translation failed: {e}. Returning original.")
            return text

    def translate_tags(self, tags: list[str]) -> list[str]:
        """
        Translate a list of YouTube tags/keywords into natural Simplified Chinese tags
        suitable for Bilibili video publishing (max 10-12 tags).
        """
        if not tags:
            return []

        # Take up to 10 tags to keep prompt concise
        sample_tags = [t.strip() for t in tags[:10] if t.strip()]
        if not sample_tags:
            return []

        system_prompt = (
            "You are an expert in Bilibili video metadata and SEO keywords.\n"
            "Translate these YouTube tags/keywords into natural Simplified Chinese tags (简体中文).\n"
            "Guidelines:\n"
            "1. Translate character/franchise/brand/game/film names into their standard Chinese names "
            "(e.g., Lightning McQueen -> 闪电麦昆, Cars -> 赛车总动员, Pixar -> 皮克斯).\n"
            "2. Keep well-known original English names if commonly searched as-is on Bilibili (e.g. Disney, Pixar).\n"
            "3. Return ONLY a comma-separated list of tags, without explanation, numbers, or markdown."
        )

        user_prompt = f"Translate these tags to Chinese:\n{', '.join(sample_tags)}"

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
            response = requests.post(self.api_url, json=payload, timeout=120)
            response.raise_for_status()
            data = response.json()
            content = data["message"]["content"].strip()
            # Split by comma (standard, Chinese comma, or newlines)
            raw_tokens = re.split(r"[,，、\n]+", content)
            translated_tags = []
            for token in raw_tokens:
                clean_tag = re.sub(r"[#\"'“”]", "", token).strip()
                if clean_tag and len(clean_tag) <= 20 and clean_tag not in translated_tags:
                    translated_tags.append(clean_tag)
            return translated_tags[:10]
        except Exception as e:
            logger.warning(f"Tags translation failed ({e}). Using raw sanitized tags.")
            sanitized = []
            for t in sample_tags:
                clean_t = re.sub(r"[#\"'“”]", "", t).strip()
                if clean_t and len(clean_t) <= 20 and clean_t not in sanitized:
                    sanitized.append(clean_t)
            return sanitized[:8]
