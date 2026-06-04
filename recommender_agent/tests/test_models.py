import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from app.models.base import engine, init_db
from app.models.user import User
from app.models.item import Item
from app.models.org_unit import OrgUnit


class TestModels:
    def setup_method(self):
        init_db()

    def test_create_user(self):
        with Session(engine) as session:
            user = User(username="test_user")
            session.add(user)
            session.commit()
            assert user.id is not None
            assert user.username == "test_user"

    def test_create_item(self):
        with Session(engine) as session:
            item = Item(title="张三", category="计算机学院")
            session.add(item)
            session.commit()
            assert item.id is not None
            assert item.title == "张三"

    def test_create_org_unit(self):
        with Session(engine) as session:
            org = OrgUnit(name="计算机学院", url="http://cs.example.com")
            session.add(org)
            session.commit()
            assert org.id is not None
            assert org.name == "计算机学院"
