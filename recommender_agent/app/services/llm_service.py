from typing import List, Optional, Dict, Any
from collections import defaultdict
import httpx
import json
import time

from app.core.config import get_settings
from app.core.exceptions import LLMCallError

settings = get_settings()


class LLMService:
    """Service for LLM API calls with fallback support"""

    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=settings.LLM_BASE_URL,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
        )
        self.chat_client = httpx.AsyncClient(
            base_url=settings.LLM_BASE_URL,
            timeout=60.0,
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
        )

    async def parse_intent(self, query: str) -> Dict[str, Any]:
        """Parse user query to structured intent"""
        if not settings.ENABLE_LLM or not settings.LLM_API_KEY:
            return self._rule_parse_intent(query)

        prompt = self._build_intent_prompt(query)

        try:
            response = await self.client.post(
                "/chat/completions",
                json={
                    "model": settings.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": "You are a query intent parser."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 500,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            intent = json.loads(content)
            return {
                "intent": intent.get("intent", ""),
                "keywords": intent.get("keywords", []),
                "tags": intent.get("tags", []),
                "org_unit": intent.get("org_unit", None),
                "title": intent.get("title", None),
            }
        except Exception as e:
            print(f"LLM intent parse failed: {e}, using rule-based fallback")
            return self._rule_parse_intent(query)

    async def generate_explanation(
        self, item_title: str, query: str, graph_paths: List[Dict] = None
    ) -> str:
        """Generate recommendation explanation"""
        if not settings.ENABLE_LLM or not settings.LLM_API_KEY:
            return self._template_explanation(item_title, query, graph_paths)

        prompt = self._build_explanation_prompt(item_title, query, graph_paths)

        try:
            response = await self.chat_client.post(
                "/chat/completions",
                json={
                    "model": settings.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": "You are a helpful recommendation assistant."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 300,
                },
            )
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"LLM explanation failed: {e}, using template fallback")
            return self._template_explanation(item_title, query, graph_paths)

    def _rule_parse_intent(self, query: str) -> Dict[str, Any]:
        """Rule-based intent parsing for fallback"""
        query_lower = query.lower()
        intent = {
            "intent": "general_recommendation",
            "keywords": [query],
            "tags": [],
            "org_unit": None,
            "title": None,
        }

        # Extract org unit keywords
        org_keywords = ["学院", "系", "部", "中心", "实验室", "研究所"]
        for kw in org_keywords:
            if kw in query:
                # Try to extract org name before keyword
                idx = query.find(kw)
                start = max(0, idx - 20)
                org_name = query[start:idx + len(kw)].strip()
                intent["org_unit"] = org_name
                break

        # Extract title keywords
        title_aliases = {
            "博士生导师": "博导",
            "博士导师": "博导",
            "博导": "博导",
            "硕士生导师": "硕导",
            "硕士导师": "硕导",
            "硕导": "硕导",
            "副教授": "副教授",
            "教授": "教授",
            "讲师": "讲师",
            "院士": "院士",
            "研究员": "研究员",
        }
        for keyword, title in title_aliases.items():
            if keyword in query:
                intent["title"] = title
                break

        # Extract research keywords
        research_keywords = [
            "NLP", "自然语言处理", "计算机视觉", "机器学习", "深度学习",
            "人工智能", "数据挖掘", "知识图谱", "推荐系统", "计算机科学",
            "软件工程", "网络安全", "数据库", "操作系统", "编译原理",
        ]
        for kw in research_keywords:
            if kw.lower() in query_lower:
                intent["tags"].append(kw)

        return intent

    def _template_explanation(
        self, item_title: str, query: str, graph_paths: List[Dict] = None
    ) -> str:
        """Template-based explanation for fallback"""
        explanations = [
            f"推荐{item_title}老师，因为该导师的研究方向与您查询的\"{query}\"高度匹配。",
            f"根据您的需求\"{query}\"，{item_title}老师在该领域有丰富研究经验，推荐关注。",
            f"{item_title}老师在相关领域表现突出，与您查询的\"{query}\"匹配度较高。",
        ]
        if graph_paths and len(graph_paths) > 0:
            path = graph_paths[0]
            nodes = path.get("path", [])
            if len(nodes) > 2:
                explanations.append(
                    f"推荐{item_title}老师，因为与您关注的领域通过\"{nodes[1]}\"有关联关系。"
                )

        import random
        return random.choice(explanations)

    def _build_intent_prompt(self, query: str) -> str:
        return f"""解析用户查询的意图，返回 JSON 格式：
查询: {query}

返回字段:
- "intent": 意图类型 (professor_recommendation 等)
- "keywords": 关键词列表
- "tags": 研究方向标签
- "org_unit": 学院/机构 (可选)
- "title": 职称 (可选)
"""

    def _build_explanation_prompt(self, item_title: str, query: str, graph_paths: List[Dict] = None) -> str:
        graph_info = ""
        if graph_paths:
            graph_info = f"图谱路径信息: {json.dumps(graph_paths[:2], ensure_ascii=False)}"

        return f"""为用户生成推荐理由：
用户查询: {query}
推荐导师: {item_title}
{graph_info}

请用1-2句话简洁说明推荐理由，语气友好专业。"""
