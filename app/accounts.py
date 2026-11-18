"""Create the account and its channel in the caller's transaction."""

from app.models import Channel, User


def add_user_with_channel(db, **values):
    user = User(**values)
    db.add(user)
    db.flush()
    db.add(Channel(owner_id=user.id, name=user.display_name))
    db.flush()
    return user
