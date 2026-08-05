"""SQLAlchemy engine/session for intake-service (writes patients/coverage/consents)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config import settings

# create_engine does not connect until first use, so importing this module is
# safe without a live database (CI import smoke test relies on that).
#
# hide_parameters=True (PR #20 round-6 review, defense in depth): every write
# in this service carries PHI (patients.name/dob/ssn/address/... and
# insurance_coverages.member_id/group_number). Without this, a DBAPIError's
# string form embeds the failed statement's bound parameter values verbatim
# — app.py's own error handlers already avoid logging str(e) for exactly
# this reason, but this is a second, engine-level backstop against any other
# call site (present or future) that logs a SQLAlchemy exception directly.
engine = create_engine(settings.db_url, pool_pre_ping=True, future=True, hide_parameters=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
