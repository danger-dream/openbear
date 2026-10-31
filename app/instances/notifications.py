"""Reuse the existing durable Telegram outbox with stable per-event identity."""
from app.notice_names import telegram_html
from app.webhooks.repository import one

# instance_id of notices about the access gateway itself (instance ids are UUIDs).
GATEWAY = '_gateway'


async def deliver(service, row):
    tg = getattr(service.host, 'web_task_telegram', None)
    if not tg:
        return 'suppressed'
    if row['instance_id'] != GATEWAY:
        instance = await service.row(row['owner'], row['instance_id'], active=False)
        if instance['status'] == 'archived':
            return 'suppressed'
    conversation = await one(service.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=? AND owner_chat_id=?', (row['conversation_uuid'], row['owner']))
    # The TG outbox uses this stable root as its idempotency key; a crash here
    # cannot create a second notification.
    queued = await tg.send_notice('instance-notification:'+row['id'], owner_chat_id=row['owner'], title='🧩 实例管理', text=telegram_html(row['text']), text_html=True,
                                  conversation_uuid=row['conversation_uuid'] if conversation else '',
                                  internal_chat_id=conversation['internal_chat_id'] if conversation else row['owner'])
    return 'enqueued' if queued else 'suppressed'
