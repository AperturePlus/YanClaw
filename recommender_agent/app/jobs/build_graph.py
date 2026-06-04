import networkx as nx
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models.base import engine
from app.models import Item, Tag, ItemTag, GraphEdge

settings = get_settings()


def build_knowledge_graph() -> nx.Graph:
    """Build NetworkX graph from items, tags, and org_units"""
    G = nx.Graph()

    with Session(engine) as session:
        # Add items as nodes
        items = session.exec(select(Item)).all()
        for item in items:
            G.add_node(
                f"Item_{item.id}",
                label=item.title,
                type="Item",
                org_unit=item.org_unit,
                research_areas=item.research_areas,
            )

        # Add org_units as nodes and connect items
        org_units = set(i.org_unit for i in items if i.org_unit)
        for org in org_units:
            G.add_node(f"Org_{org}", label=org, type="OrgUnit")

        for item in items:
            if item.org_unit:
                G.add_edge(
                    f"Item_{item.id}", f"Org_{item.org_unit}",
                    relation="belongs_to", weight=1.0
                )

        # Add tags as nodes and connect items
        tags = session.exec(select(Tag)).all()
        for tag in tags:
            G.add_node(f"Tag_{tag.id}", label=tag.name, type="Tag")

        item_tags = session.exec(select(ItemTag)).all()
        for it in item_tags:
            G.add_edge(
                f"Item_{it.item_id}", f"Tag_{it.tag_id}",
                relation="has_tag", weight=it.weight
            )

        # Add research area edges between items with similar areas
        for i1 in items:
            if not i1.research_areas:
                continue
            areas1 = set(i1.research_areas.lower().split())
            for i2 in items:
                if i1.id >= i2.id or not i2.research_areas:
                    continue
                areas2 = set(i2.research_areas.lower().split())
                overlap = areas1 & areas2
                if len(overlap) >= 2:
                    G.add_edge(
                        f"Item_{i1.id}", f"Item_{i2.id}",
                        relation="similar_research", weight=len(overlap) * 0.1
                    )

    return G


def save_graph_edges(G: nx.Graph):
    """Save NetworkX graph edges to graph_edges table"""
    with Session(engine) as session:
        session.query(GraphEdge).delete()
        for u, v, data in G.edges(data=True):
            edge = GraphEdge(
                source_id=u,
                source_type=G.nodes[u].get("type", ""),
                target_id=v,
                target_type=G.nodes[v].get("type", ""),
                relation=data.get("relation", "related"),
                weight=data.get("weight", 1.0),
            )
            session.add(edge)
        session.commit()


if __name__ == "__main__":
    G = build_knowledge_graph()
    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    save_graph_edges(G)
