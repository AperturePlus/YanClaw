import re
from collections import defaultdict
from typing import cast

import networkx as nx
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models import GraphEdge, Item, ItemTag, Tag
from app.models.base import engine, ensure_runtime_schema

settings = get_settings()
logger = get_logger()


def extract_research_terms(text: str | None) -> set[str]:
    if not text:
        return set()

    terms = {
        part.strip().lower()
        for part in re.split(r"[/、，,;；\n\r\t ]+", text)
        if len(part.strip()) >= 2
    }
    for keyword in [
        "人工智能",
        "自然语言处理",
        "知识图谱",
        "机器学习",
        "深度学习",
        "计算机视觉",
        "推荐系统",
        "数据挖掘",
        "网络安全",
        "软件工程",
        "航空",
        "机器人",
    ]:
        if keyword in text:
            terms.add(keyword)
    return terms


def build_knowledge_graph() -> nx.Graph[str]:
    """Build NetworkX graph from canonical items, tags, sources, and org units."""
    G = nx.Graph()

    with Session(engine) as session:
        items = session.exec(select(Item)).all()
        for item in items:
            G.add_node(
                f"Item_{item.id}",
                label=item.title,
                type="Item",
                entity_id=item.entity_id,
                source_id=item.source_id,
                org_unit=item.org_unit,
                research_areas=item.research_areas,
            )

        source_ids = set(item.source_id for item in items if item.source_id)
        for source_id in source_ids:
            G.add_node(f"Source_{source_id}", label=source_id, type="SourceSystem")

        org_units = set(item.org_unit for item in items if item.org_unit)
        for org in org_units:
            G.add_node(f"Org_{org}", label=org, type="OrgUnit")

        for item in items:
            if item.source_id:
                G.add_edge(
                    f"Item_{item.id}",
                    f"Source_{item.source_id}",
                    relation="from_source",
                    weight=1.0,
                    data_source_id=item.source_id,
                )
            if item.org_unit:
                G.add_edge(
                    f"Item_{item.id}",
                    f"Org_{item.org_unit}",
                    relation="belongs_to",
                    weight=1.0,
                    data_source_id=item.source_id,
                )

        tags = session.exec(select(Tag)).all()
        for tag in tags:
            G.add_node(f"Tag_{tag.id}", label=tag.name, type="Tag")

        item_tags = session.exec(select(ItemTag)).all()
        for item_tag in item_tags:
            G.add_edge(
                f"Item_{item_tag.item_id}",
                f"Tag_{item_tag.tag_id}",
                relation="has_tag",
                weight=item_tag.weight,
            )

        item_terms: dict[int, set[str]] = {}
        items_by_term: dict[str, list[int]] = defaultdict(list)
        for item in items:
            item_id = cast(int, item.id)
            terms = extract_research_terms(item.research_areas)
            item_terms[item_id] = terms
            for term in terms:
                term_node = f"ResearchArea_{term}"
                G.add_node(term_node, label=term, type="ResearchArea")
                G.add_edge(
                    f"Item_{item.id}",
                    term_node,
                    relation="has_research_area",
                    weight=1.0,
                    data_source_id=item.source_id,
                )
                items_by_term[term].append(item_id)

        candidate_pairs = set()
        for item_ids in items_by_term.values():
            for index, left in enumerate(item_ids):
                for right in item_ids[index + 1:]:
                    candidate_pairs.add((min(left, right), max(left, right)))

        for left, right in candidate_pairs:
            overlap = item_terms.get(left, set()) & item_terms.get(right, set())
            if overlap:
                G.add_edge(
                    f"Item_{left}",
                    f"Item_{right}",
                    relation="similar_research",
                    weight=min(1.0, 0.2 * len(overlap)),
                    evidence=",".join(sorted(overlap)),
                )

    return G


def save_graph_edges(G: nx.Graph[str]):
    """Save NetworkX graph edges to graph_edges table."""
    ensure_runtime_schema()
    with Session(engine) as session:
        session.query(GraphEdge).delete()
        for source, target, data in G.edges(data=True):
            edge = GraphEdge(
                source_id=source,
                source_type=G.nodes[source].get("type", ""),
                target_id=target,
                target_type=G.nodes[target].get("type", ""),
                relation=data.get("relation", "related"),
                weight=data.get("weight", 1.0),
                evidence=data.get("evidence"),
                data_source_id=data.get("data_source_id"),
            )
            session.add(edge)
        session.commit()


if __name__ == "__main__":
    graph = build_knowledge_graph()
    logger.info("Graph: %s nodes, %s edges", graph.number_of_nodes(), graph.number_of_edges())
    save_graph_edges(graph)
