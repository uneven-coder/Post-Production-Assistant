from typing import Optional, Any, Dict
from dataclasses import dataclass, field


@dataclass
class ResponseInfo:
    total_cost: float = 0.0
    generation_time: float = 0.0
    model: Optional[str] = None
    provider: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.total_cost < 0:
            self.total_cost = 0.0
        if self.generation_time < 0:
            self.generation_time = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResponseInfo":
        known = {"total_cost", "generation_time", "model", "provider",
                 "prompt_tokens", "completion_tokens", "total_tokens", "error"}
        return cls(
            **{k: v for k, v in data.items() if k in known},
            extra={k: v for k, v in data.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "total_cost": self.total_cost,
            "generation_time": self.generation_time,
        }
        for k in ("model", "provider", "error"):
            if getattr(self, k):
                result[k] = getattr(self, k)
        if self.prompt_tokens > 0:
            result["prompt_tokens"] = self.prompt_tokens
            result["completion_tokens"] = self.completion_tokens
            result["total_tokens"] = self.total_tokens
        result.update(self.extra)
        return result

    def format(self) -> str:
        if self.error:
            return f"Error: {self.error}"
        lines = []
        if self.model:
            lines.append(f"Model: {self.model}")
        if self.provider:
            lines.append(f"Provider: {self.provider}")
        lines.append(f"Generation Time: {self.generation_time:.2f} seconds")
        lines.append(f"Total Cost: ${self.total_cost:.6f}")
        if self.total_tokens > 0:
            lines.append(f"Prompt Tokens: {self.prompt_tokens}")
            lines.append(f"Completion Tokens: {self.completion_tokens}")
            lines.append(f"Total Tokens: {self.total_tokens}")
        for k, v in self.extra.items():
            if isinstance(v, (int, float, str)):
                lines.append(f"{k.replace('_', ' ').title()}: {v}")
        return "\n".join(lines)

    def __add__(self, other: "ResponseInfo") -> "ResponseInfo":
        return ResponseInfo(
            total_cost=self.total_cost + other.total_cost,
            generation_time=self.generation_time + other.generation_time,
            model=self.model or other.model,
            provider=self.provider or other.provider,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            error=self.error or other.error,
            extra={**self.extra, **other.extra},
        )


@dataclass
class AggregatedResponseInfo:
    total_cost: float = 0.0
    total_time: float = 0.0
    categories: Dict[str, ResponseInfo] = field(default_factory=dict)
    items: list = field(default_factory=list)

    def add(self, info: ResponseInfo, category: Optional[str] = None):
        self.items.append(info)
        self.total_cost += info.total_cost
        self.total_time += info.generation_time
        if category:
            self.categories[category] = self.categories.get(category, ResponseInfo()) + info

    def format(self) -> str:
        lines = [f"Total Cost: ${self.total_cost:.6f}", f"Total Time: {self.total_time:.2f}s"]
        for cat, info in self.categories.items():
            lines.append(f"\n{cat.replace('_', ' ').title()}:")
            lines.append(f"  Cost: ${info.total_cost:.6f}")
            lines.append(f"  Time: {info.generation_time:.2f}s")
        return "\n".join(lines)
