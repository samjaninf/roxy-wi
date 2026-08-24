from app.modules.db.db_model import ConfigChangeTarget, connect


def up():
    """Create per-server rollout records for Change Center."""
    connection = connect()
    connection.create_tables([ConfigChangeTarget], safe=True)


def down():
    """Remove per-server rollout records."""
    connection = connect()
    connection.drop_tables([ConfigChangeTarget], safe=True)
