from models import SessionLocal, Room

session = SessionLocal()

existing = session.query(Room).count()
print(f"Rooms currently in database: {existing}")

room_data = [
    ("101", "Standard"),
    ("102", "Standard"),
    ("103", "Deluxe"),
    ("104", "Deluxe"),
    ("105", "Suite"),
    ("106", "Suite"),
]

for number, room_type in room_data:
    already_exists = session.query(Room).filter(Room.room_number == number).first()
    if not already_exists:
        new_room = Room(room_number=number, room_type=room_type)
        session.add(new_room)
        print(f"Added room {number} ({room_type})")
    else:
        print(f"Room {number} already exists, skipping")

session.commit()
session.close()

print("Done. Total rooms now:", SessionLocal().query(Room).count())