from models import SessionLocal, Room, Booking
from datetime import date

session = SessionLocal()

# Add a room first (only need to do this once)
room = Room(room_number="101", room_type="Deluxe")
session.add(room)
session.commit()

# Add a booking for that room
booking = Booking(
    room_id=room.id,
    guest_name="Rahul",
    guest_phone="9876543210",
    check_in=date(2026, 8, 15),
    check_out=date(2026, 8, 17),
    status="confirmed"
)
session.add(booking)
session.commit()

print("Room ID created:", room.id)
print("Booking added for room", room.id)

session.close()