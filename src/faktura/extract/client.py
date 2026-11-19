"""Vision clients. The rest of the code depends only on `VisionClient`."""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel

from faktura.extract.schema import EXTRACTION_PROMPT, ExtractedInvoice

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ExtractionResult:
    invoice: ExtractedInvoice
    usage: Usage
    latency_s: float
    model: str


class VisionClient(Protocol):
    model: str

    def extract(self, image_png: bytes) -> ExtractionResult: ...


class AzureResponsesClient:
    """Azure AI Foundry deployment (e.g. gpt-6-luna) via the v1 Responses API."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        reasoning_effort: str | None = "low",
        timeout_s: float = 120.0,
    ) -> None:
        from openai import OpenAI  # imported here so tests never need network config

        # Retries back off and honour Retry-After, which absorbs 429s from small quotas.
        self._client = OpenAI(base_url=endpoint, api_key=api_key, timeout=timeout_s, max_retries=8)
        self.model = model
        self._reasoning = {"effort": reasoning_effort} if reasoning_effort else None

    @classmethod
    def from_env(cls) -> AzureResponsesClient:
        missing = [
            k
            for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "FAKTURA_VISION_MODEL")
            if not os.environ.get(k)
        ]
        if missing:
            raise RuntimeError(f"missing environment variables: {', '.join(missing)} (see .env)")
        return cls(
            endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            model=os.environ["FAKTURA_VISION_MODEL"],
            reasoning_effort=os.environ.get("FAKTURA_REASONING_EFFORT", "low") or None,
        )

    def parse_image(
        self,
        image: bytes,
        mime: str,
        instructions: str,
        user_text: str,
        text_format: type[T],
        detail: str | None = None,
        reasoning: str | None = None,
    ) -> tuple[T, Usage, float]:
        """One structured-output call on one image. Returns (parsed, usage, latency_s).

        `detail` ("low" | "high" | "auto") is passed through to the image input; None
        leaves the API default. `reasoning` overrides the client's default effort.
        """
        return self.parse_images(
            [(image, mime)], instructions, user_text, text_format, detail, reasoning
        )

    def parse_images(
        self,
        images: list[tuple[bytes, str]],
        instructions: str,
        user_text: str,
        text_format: type[T],
        detail: str | None = None,
        reasoning: str | None = None,
    ) -> tuple[T, Usage, float]:
        """Like parse_image, with several images (e.g. a page plus zoomed tiles) in order."""
        image_parts = []
        for data, mime in images:
            b64 = base64.b64encode(data).decode("ascii")
            part = {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"}
            if detail:
                part["detail"] = detail
            image_parts.append(part)
        effort = {"effort": reasoning} if reasoning else self._reasoning
        kwargs = {"reasoning": effort} if effort else {}
        t0 = time.perf_counter()
        resp = self._client.responses.parse(
            model=self.model,
            instructions=instructions,
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_text}, *image_parts],
                }
            ],
            text_format=text_format,
            **kwargs,
        )
        latency = time.perf_counter() - t0
        parsed = resp.output_parsed
        if parsed is None:
            raise RuntimeError(f"model returned no parsed output (status={resp.status})")
        u = resp.usage
        cached = 0
        if u is not None and u.input_tokens_details is not None:
            cached = u.input_tokens_details.cached_tokens or 0
        usage = Usage(
            input_tokens=u.input_tokens if u else 0,
            cached_input_tokens=cached,
            output_tokens=u.output_tokens if u else 0,
        )
        return parsed, usage, latency

    def extract(self, image_png: bytes) -> ExtractionResult:
        parsed, usage, latency = self.parse_image(
            image_png, "image/png", EXTRACTION_PROMPT, "Extract this invoice.", ExtractedInvoice
        )
        return ExtractionResult(parsed, usage, latency, self.model)


class FakeClient:
    """Returns a fixed extraction per call. For tests and offline pipeline runs."""

    def __init__(self, responses: list[ExtractedInvoice], model: str = "fake") -> None:
        self._responses = list(responses)
        self.model = model

    def extract(self, image_png: bytes) -> ExtractionResult:
        if not self._responses:
            raise RuntimeError("FakeClient ran out of responses")
        return ExtractionResult(self._responses.pop(0), Usage(100, 0, 50), 0.01, self.model)
