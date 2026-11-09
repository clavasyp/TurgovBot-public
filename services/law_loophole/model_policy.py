# START OF FILE services/law_loophole/model_policy.py

"""
Model Policy для анализатора дыр законки.

Не используется одна глобальная модель. Профиль выбирается из:
- fast       — быстрый flash-пайплайн;
- balanced   — стандартный flash-пайплайн;
- deep       — pro для analyzer/synthesis;
- expert     — pro + batch mode.

Реальные имена моделей берутся из config.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import config
from .bundle import LawContextBundle, ModelProfile, SelectionMode

_VALID_PROFILES = {"fast", "balanced", "deep", "expert"}


@dataclass(frozen=True)
class LoopholeModels:
    profile: ModelProfile
    router_model: str
    analyzer_model: str
    synthesis_model: str
    batch_mode: bool


def get_loophole_models(profile: ModelProfile) -> LoopholeModels:
    """Возвращает имена моделей для заданного профиля."""
    if profile == "fast":
        return LoopholeModels(
            profile="fast",
            router_model=config.LOOPHOLE_ROUTER_MODEL_FAST,
            analyzer_model=config.LOOPHOLE_ANALYZER_MODEL_FAST,
            synthesis_model=config.LOOPHOLE_SYNTHESIS_MODEL_FAST,
            batch_mode=False,
        )
    if profile == "balanced":
        return LoopholeModels(
            profile="balanced",
            router_model=config.LOOPHOLE_ROUTER_MODEL_BALANCED,
            analyzer_model=config.LOOPHOLE_ANALYZER_MODEL_BALANCED,
            synthesis_model=config.LOOPHOLE_SYNTHESIS_MODEL_BALANCED,
            batch_mode=False,
        )
    if profile == "deep":
        return LoopholeModels(
            profile="deep",
            router_model=config.LOOPHOLE_ROUTER_MODEL_DEEP,
            analyzer_model=config.LOOPHOLE_ANALYZER_MODEL_DEEP,
            synthesis_model=config.LOOPHOLE_SYNTHESIS_MODEL_DEEP,
            batch_mode=False,
        )
    if profile == "expert":
        return LoopholeModels(
            profile="expert",
            router_model=config.LOOPHOLE_ROUTER_MODEL_EXPERT,
            analyzer_model=config.LOOPHOLE_ANALYZER_MODEL_EXPERT,
            synthesis_model=config.LOOPHOLE_SYNTHESIS_MODEL_EXPERT,
            batch_mode=True,
        )
    raise ValueError(f"Неизвестный model_profile: {profile}")


def choose_model_profile(
    *,
    requested_profile: ModelProfile,
    selection_mode: SelectionMode,
    estimated_size: int,
    doc_count: int,
) -> ModelProfile:
    """
    Подбирает финальный профиль с учетом размера и режима выборки.

    Правила:
        - full_server при большом контексте принудительно повышается до deep/expert;
        - fast не разрешает слишком большую выборку — поднимаем до balanced;
        - очень большой контекст требует expert (для batch mode).
    """
    if requested_profile not in _VALID_PROFILES:
        raise ValueError(f"Неизвестный model_profile: {requested_profile}")

    profile: ModelProfile = requested_profile

    big_threshold = config.LOOPHOLE_BATCH_THRESHOLD_CHARS
    huge_threshold = big_threshold * 3

    if selection_mode == "full_server":
        if profile in ("fast", "balanced") and (
            estimated_size > big_threshold or doc_count > config.LOOPHOLE_MAX_MANUAL_DOCS_BALANCED
        ):
            profile = "deep"
        if estimated_size > huge_threshold:
            profile = "expert"

    if profile == "fast" and doc_count > config.LOOPHOLE_MAX_MANUAL_DOCS_FAST:
        profile = "balanced"

    if estimated_size > huge_threshold and profile != "expert":
        profile = "expert"

    return profile


def reconcile_bundle_profile(bundle: LawContextBundle) -> LawContextBundle:
    """
    Приводит profile внутри bundle в соответствие с фактическим размером.
    Возвращает тот же объект (мутация на месте).
    """
    bundle.model_profile = choose_model_profile(
        requested_profile=bundle.model_profile,
        selection_mode=bundle.selection_mode,
        estimated_size=bundle.estimated_size,
        doc_count=len(bundle.selected_laws),
    )
    return bundle


# END OF FILE services/law_loophole/model_policy.py
