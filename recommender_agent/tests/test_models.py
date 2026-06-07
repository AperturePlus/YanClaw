from sqlmodel import Session

from app.models.user import User
from app.models.item import Item
from app.models.org_unit import OrgUnit


class TestModels:
    def test_create_user(self, db_session: Session) -> None:
        user = User(username="test_user")
        db_session.add(user)
        db_session.commit()
        assert user.id is not None
        assert user.username == "test_user"

    def test_create_item(self, db_session: Session) -> None:
        item = Item(title="张三", category="计算机学院")
        db_session.add(item)
        db_session.commit()
        assert item.id is not None
        assert item.title == "张三"

    def test_create_org_unit(self, db_session: Session) -> None:
        org = OrgUnit(name="计算机学院", url="http://cs.example.com")
        db_session.add(org)
        db_session.commit()
        assert org.id is not None
        assert org.name == "计算机学院"
