from uuid import UUID

from sqlalchemy import delete, exists, false, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.channels.schemas import ChannelList, ChannelView, SubscriptionState
from app.errors import DomainError
from app.models import Channel, Subscription
from app.pagination import decode_cursor, encode_cursor


def get_channel(db, identifier: UUID, *, owner_id=None, lock=False):
    query = select(Channel).where(Channel.id == identifier)
    if owner_id is not None:
        query = query.where(Channel.owner_id == owner_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    channel = db.scalar(query)
    if channel is None:
        raise DomainError(404, "channel_not_found", "Канал не найден")
    return channel


def my_channel(db, owner_id):
    channel = db.scalar(select(Channel).where(Channel.owner_id == owner_id))
    if channel is None:
        raise DomainError(404, "channel_not_found", "Канал не найден")
    return channel


def profile_query(viewer_id):
    count = (
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.channel_id == Channel.id)
        .correlate(Channel)
        .scalar_subquery()
    )
    subscribed = (
        exists()
        .where(Subscription.channel_id == Channel.id, Subscription.subscriber_id == viewer_id)
        .correlate(Channel)
        if viewer_id
        else false()
    )
    return select(Channel, count.label("subscriber_count"), subscribed)


def output(row):
    channel, count, subscribed = row
    return ChannelView(
        id=channel.id,
        owner_id=channel.owner_id,
        name=channel.name,
        description=channel.description,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
        subscriber_count=count,
        is_subscribed=subscribed,
    )


def profile(db, identifier, viewer_id):
    row = db.execute(profile_query(viewer_id).where(Channel.id == identifier)).first()
    if row is None:
        raise DomainError(404, "channel_not_found", "Канал не найден")
    return output(row)


def patch(db, identifier, owner_id, payload):
    channel = get_channel(db, identifier, owner_id=owner_id, lock=True)
    for name, value in payload.model_dump(exclude_unset=True).items():
        setattr(channel, name, value)
    db.commit()
    return profile(db, identifier, owner_id)


def subscribe(db, identifier, subscriber_id, *, enabled):
    channel = get_channel(db, identifier, lock=True)
    if enabled:
        if channel.owner_id == subscriber_id:
            raise DomainError(409, "self_subscription", "Нельзя подписаться на свой канал")
        db.execute(
            insert(Subscription)
            .values(channel_id=identifier, subscriber_id=subscriber_id)
            .on_conflict_do_nothing()
        )
    else:
        db.execute(
            delete(Subscription).where(
                Subscription.channel_id == identifier, Subscription.subscriber_id == subscriber_id
            )
        )
    db.commit()
    current = profile(db, identifier, subscriber_id)
    return SubscriptionState(
        channel_id=identifier,
        is_subscribed=current.is_subscribed,
        subscriber_count=current.subscriber_count,
    )


def list_subscriptions(db, subscriber_id, limit, cursor):
    scope = f"subscriptions:{subscriber_id}"
    position = decode_cursor(cursor, scope)
    query = (
        profile_query(subscriber_id)
        .join(Subscription, Subscription.channel_id == Channel.id)
        .where(Subscription.subscriber_id == subscriber_id)
    )
    if position:
        query = query.where(tuple_(Subscription.created_at, Subscription.channel_id) < position)
    rows = db.execute(
        query.add_columns(Subscription.created_at)
        .order_by(Subscription.created_at.desc(), Subscription.channel_id.desc())
        .limit(limit + 1)
    ).all()
    page = rows[:limit]
    next_cursor = encode_cursor(scope, page[-1][3], page[-1][0].id) if len(rows) > limit else None
    return ChannelList(items=[output(row[:3]) for row in page], next_cursor=next_cursor)
