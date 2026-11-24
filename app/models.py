from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Counter(Base):
    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
