from sqlalchemy import create_engine, Column, Integer, String, Date
from sqlalchemy.orm import declarative_base, sessionmaker

engine = create_engine("sqlite:///resort.db")
Base = declarative_base()

class Room(Base):
    __tablename__ = "rooms"
    id = Column(Integer, primary_key=True)
    room_number = Column(String, unique=True)
    room_type = Column(String)

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer)
    guest_name = Column(String)
    guest_phone = Column(String)
    check_in = Column(Date)
    check_out = Column(Date)
    status = Column(String, default="confirmed")

Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)