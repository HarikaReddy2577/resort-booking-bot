from models import SessionLocal, Booking
from datetime import date

def has_conflict(room_id, check_in, check_out):
    session = SessionLocal()
    
    overlapping = session.query(Booking).filter(
        Booking.room_id == room_id,
        Booking.status == "confirmed",
        Booking.check_in < check_out,
        Booking.check_out > check_in
    ).first()
    
    session.close()
    return overlapping is not None

if __name__ == "__main__":
    result = has_conflict(1, date(2026, 8, 15), date(2026, 8, 17))
    print("Conflict found?", result)