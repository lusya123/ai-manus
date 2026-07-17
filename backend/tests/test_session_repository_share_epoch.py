from types import SimpleNamespace

from app.infrastructure.models.documents import SessionDocument
from app.infrastructure.repositories.mongo_session_repository import (
    MongoSessionRepository,
)


class _LegacySessionDocument:
    def __init__(self, document_id="mongo-id", share_epoch=None):
        self.id = document_id
        self.share_epoch = share_epoch

    def to_domain(self):
        return SimpleNamespace(share_epoch=self.share_epoch)


async def test_legacy_share_epoch_is_persisted_once_and_reused(monkeypatch):
    class Collection:
        def __init__(self):
            self.persisted = None
            self.updates = 0

        async def update_one(self, query, update):
            self.updates += 1
            self.persisted = update["$set"]["share_epoch"]
            return SimpleNamespace(modified_count=1)

        async def find_one(self, query, projection):
            return {"share_epoch": self.persisted}

    collection = Collection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    repository = MongoSessionRepository()
    first = _LegacySessionDocument()

    first_domain = await repository._to_domain_with_share_epoch(first)
    second = _LegacySessionDocument(share_epoch=collection.persisted)
    second_domain = await repository._to_domain_with_share_epoch(second)

    assert first_domain.share_epoch == second_domain.share_epoch
    assert len(first_domain.share_epoch) == 32
    assert collection.updates == 1


async def test_concurrent_legacy_backfill_uses_winning_persisted_epoch(
    monkeypatch,
):
    class Collection:
        async def update_one(self, query, update):
            return SimpleNamespace(modified_count=0)

        async def find_one(self, query, projection):
            return {"share_epoch": "winner-epoch"}

    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: Collection()),
    )

    domain = await MongoSessionRepository()._to_domain_with_share_epoch(
        _LegacySessionDocument()
    )

    assert domain.share_epoch == "winner-epoch"
