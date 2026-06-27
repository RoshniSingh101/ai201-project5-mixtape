"""
tests/test_notifications.py — Mixtape

Regression tests for notification creation.

The test below would have caught Issue #4: rate_song saved a rating but
never notified the song's original sharer, while add_to_playlist did.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Neon City", artist="Synthwave Co", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_notifies_song_sharer(app, seed):
    """When a friend rates a shared song, the sharer gets a 'song_rated' notification."""
    with app.app_context():
        sharer_id = seed["sharer"].id
        rater_id = seed["rater"].id
        song_id = seed["song"].id

        assert get_notifications(sharer_id) == []

        rate_song(rater_id, song_id, 5)

        notifs = get_notifications(sharer_id)
        assert len(notifs) == 1  # Bug #4 left this at 0
        assert notifs[0]["type"] == "song_rated"


def test_self_rating_does_not_notify(app, seed):
    """Rating your own song should not generate a notification."""
    with app.app_context():
        sharer_id = seed["sharer"].id
        song_id = seed["song"].id

        rate_song(sharer_id, song_id, 4)

        assert get_notifications(sharer_id) == []
