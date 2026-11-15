"""测试夹具：内存 SQLite 上的 service 层冒烟环境（不依赖 MySQL / Docker）。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def memory_db(monkeypatch):
    from src.config import database as database_module
    from src.models import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", session_factory)
    Base.metadata.create_all(bind=engine)

    from scripts import seed_demo

    monkeypatch.setattr(seed_demo, "session_scope", database_module.session_scope)
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded(memory_db, monkeypatch):
    from scripts import seed_demo

    seed_demo.seed_all()
    return memory_db
