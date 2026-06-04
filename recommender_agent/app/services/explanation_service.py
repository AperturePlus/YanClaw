from typing import Optional, List, Dict, Any
import random

from app.core.config import get_settings

settings = get_settings()


class ExplanationService:
    """Service for generating recommendation explanations"""

    _EXPLANATION_TEMPLATES = {
        "semantic_match": [
            "推荐{name}老师，因为其研究方向与您查询的\"{query}\"高度匹配。",
            "根据您的需求\"{query}\"，{name}老师在相关领域有丰富研究经验。",
            "{name}老师的研究与\"{query}\"关联度较高，值得深入了解。",
        ],
        "graph_match": [
            "推荐{name}老师，因为其与您的关注的其他领域存在关联。",
            "{name}老师在相关研究方向上与您已有兴趣有相似性。",
        ],
        "popular": [
            "{name}老师在该领域影响力较大，是热门推荐。",
            "{name}老师当前关注度高，推荐作为参考。",
        ],
    }

    @staticmethod
    def generate_template_explanation(
        item_title: str, query: str, sources: List[str]
    ) -> str:
        """Generate explanation using templates"""
        templates = []

        if "vector" in sources:
            templates.extend(ExplanationService._EXPLANATION_TEMPLATES["semantic_match"])
        if "graph" in sources:
            templates.extend(ExplanationService._EXPLANATION_TEMPLATES["graph_match"])
        if not templates:
            templates.extend(ExplanationService._EXPLANATION_TEMPLATES["popular"])

        template = random.choice(templates)
        return template.format(name=item_title, query=query)

    @staticmethod
    def generate_combined_explanation(
        item_title: str,
        query: str,
        semantic_score: float = 0,
        graph_score: float = 0,
        org_unit: Optional[str] = None,
    ) -> str:
        """Generate combined explanation with all signals"""
        parts = []

        if semantic_score > 0.7:
            parts.append(f"研究方向与您查询的\"{query}\"高度匹配")
        elif semantic_score > 0.4:
            parts.append(f"研究方向与您查询的\"{query}\"部分相关")

        if graph_score > 0.5:
            parts.append("与您的兴趣图谱存在较强关联")

        if org_unit:
            parts.append(f"隶属于{org_unit}")

        if not parts:
            parts.append("综合多方面因素推荐")

        return f"推荐{item_title}老师，{', '.join(parts)}。"
