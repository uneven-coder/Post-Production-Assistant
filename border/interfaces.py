from abc import ABC, abstractmethod
import numpy as np


class IImageProcessor(ABC):
    @abstractmethod
    def process(self, image: np.ndarray) -> np.ndarray:
        pass


class IPatchLoader(ABC):
    @abstractmethod
    def load(self, path: str) -> dict:
        pass


class IBorderApplicator(ABC):
    @abstractmethod
    def apply_border(self, content: np.ndarray, patches: dict, scale: float) -> np.ndarray:
        pass


class IVideoProcessor(ABC):
    @abstractmethod
    def process_video(self, input_path: str, output_path: str, **kwargs) -> str:
        pass
