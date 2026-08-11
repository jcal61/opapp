from sqlalchemy import String, ForeignKey, Boolean, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)

    # Setup / customization
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(50))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    timezone: Mapped[str | None] = mapped_column(String(60), default="America/Chicago")
    closeout_hour: Mapped[int | None] = mapped_column(Integer, default=4)  # business day ends at this hour
    phone: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)

    parent = relationship("Location", remote_side=[id], backref="children")
    stock_levels = relationship("StockLevel", back_populates="location")
    counts = relationship("InventoryCount", back_populates="location")

    def display_label(self) -> str:
        return f"{self.name} ({self.code})" if self.code else self.name
