import pytest

from app.services.llm_service import LLMService


class TestLLMService:
    def test_rule_parse_intent(self):
        service = LLMService()
        intent = service._rule_parse_intent("NLP方向的教授")
        assert "NLP" in intent["tags"]
        assert "教授" in [intent.get("title", "")]

    def test_rule_parse_intent_org_unit(self):
        service = LLMService()
        intent = service._rule_parse_intent("计算机学院的博士生导师")
        assert intent.get("org_unit") is not None
        assert "博导" == intent.get("title")

    def test_template_explanation(self):
        service = LLMService()
        explanation = service._template_explanation("张三", "NLP方向", [])
        assert "张三" in explanation
        assert "NLP" in explanation
