from sqlalchemy.orm import Session

from db.session import get_session


def get_db() -> Session:
    session = get_session()
    try:
        yield session
    finally:
        session.close()
