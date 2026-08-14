import os
from dotenv import load_dotenv
from twilio.rest import Client
from fastapi import FastAPI, HTTPException, Form
from fastapi.responses import PlainTextResponse, HTMLResponse
from pydantic import BaseModel
from datetime import date as date_type
import calendar
from models import SessionLocal, Booking, Room
from conflict_check import has_conflict

load_dotenv()
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_NUMBER") or os.getenv("TWILIO_WHATSAPP_FROM")
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

APP_BASE_URL = os.getenv("APP_BASE_URL", "https://resort-booking-bot.onrender.com")

app = FastAPI()

SESSIONS = {}

class BookingRequest(BaseModel):
    room_id: int
    guest_name: str
    guest_phone: str
    check_in: date_type
    check_out: date_type
    from_whatsapp: str | None = None


class ModifyBookingRequest(BaseModel):
    check_in: date_type
    check_out: date_type


@app.get("/")
def home():
    return {"message": "Resort booking bot is running"}


@app.get("/seed-rooms-once")
def seed_rooms_once():
    session = SessionLocal()
    existing = session.query(Room).count()
    if existing > 0:
        session.close()
        return {"message": f"Rooms already exist ({existing}), skipping."}

    room_data = [
        ("101", "Standard"),
        ("102", "Standard"),
        ("103", "Deluxe"),
        ("104", "Deluxe"),
        ("105", "Suite"),
        ("106", "Suite"),
    ]
    for number, room_type in room_data:
        session.add(Room(room_number=number, room_type=room_type))
    session.commit()
    session.close()
    return {"message": "6 rooms added successfully."}


@app.post("/bookings")
def create_booking(booking: BookingRequest):
    if has_conflict(booking.room_id, booking.check_in, booking.check_out):
        raise HTTPException(status_code=409, detail="Room already booked for these dates")

    session = SessionLocal()
    new_booking = Booking(
        room_id=booking.room_id,
        guest_name=booking.guest_name,
        guest_phone=booking.guest_phone,
        check_in=booking.check_in,
        check_out=booking.check_out,
        status="confirmed"
    )
    session.add(new_booking)
    session.commit()
    session.refresh(new_booking)
    session.close()

    if booking.from_whatsapp:
        try:
            twilio_client.messages.create(
                from_=TWILIO_FROM,
                to=booking.from_whatsapp,
                body=f"Booking confirmed via calendar.\nBooking ID: BK{new_booking.id:04d}\nRoom {booking.room_id}, {booking.check_in} to {booking.check_out}"
            )
        except Exception as e:
            print("Failed to send WhatsApp confirmation:", e)

    return {"message": "Booking confirmed", "booking_id": new_booking.id}


@app.get("/availability")
def check_availability(room_id: int, check_in: date_type, check_out: date_type):
    conflict = has_conflict(room_id, check_in, check_out)
    return {
        "room_id": room_id,
        "check_in": check_in,
        "check_out": check_out,
        "available": not conflict
    }


@app.delete("/bookings/{booking_id}")
def cancel_booking(booking_id: int):
    session = SessionLocal()
    booking = session.query(Booking).filter(Booking.id == booking_id).first()

    if not booking:
        session.close()
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status == "cancelled":
        session.close()
        raise HTTPException(status_code=400, detail="Booking already cancelled")

    booking.status = "cancelled"
    session.commit()
    session.close()

    return {"message": "Booking cancelled", "booking_id": booking_id}


@app.patch("/bookings/{booking_id}")
def modify_booking(booking_id: int, update: ModifyBookingRequest):
    session = SessionLocal()
    booking = session.query(Booking).filter(Booking.id == booking_id).first()

    if not booking:
        session.close()
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status != "confirmed":
        session.close()
        raise HTTPException(status_code=400, detail="Cannot modify a cancelled booking")

    conflicting = session.query(Booking).filter(
        Booking.room_id == booking.room_id,
        Booking.status == "confirmed",
        Booking.id != booking_id,
        Booking.check_in < update.check_out,
        Booking.check_out > update.check_in
    ).first()

    if conflicting:
        session.close()
        raise HTTPException(status_code=409, detail="New dates conflict with another booking")

    booking.check_in = update.check_in
    booking.check_out = update.check_out
    session.commit()
    session.close()

    return {"message": "Booking updated", "booking_id": booking_id}


@app.get("/arrivals-today")
def arrivals_today():
    session = SessionLocal()
    today = date_type.today()
    arrivals = session.query(Booking).filter(
        Booking.check_in == today,
        Booking.status == "confirmed"
    ).all()
    session.close()

    return [
        {"booking_id": b.id, "room_id": b.room_id, "guest_name": b.guest_name, "guest_phone": b.guest_phone}
        for b in arrivals
    ]


@app.get("/departures-today")
def departures_today():
    session = SessionLocal()
    today = date_type.today()
    departures = session.query(Booking).filter(
        Booking.check_out == today,
        Booking.status == "confirmed"
    ).all()
    session.close()

    return [
        {"booking_id": b.id, "room_id": b.room_id, "guest_name": b.guest_name, "guest_phone": b.guest_phone}
        for b in departures
    ]


@app.get("/day-details")
def day_details(date: date_type):
    session = SessionLocal()
    all_rooms = session.query(Room).all()
    bookings = session.query(Booking).filter(
        Booking.status == "confirmed",
        Booking.check_in <= date,
        Booking.check_out > date
    ).all()
    booked_room_ids = {b.room_id for b in bookings}
    booked_list = [
        {"room_id": b.room_id, "guest_name": b.guest_name, "guest_phone": b.guest_phone, "booking_id": b.id}
        for b in bookings
    ]
    available_rooms = [r.id for r in all_rooms if r.id not in booked_room_ids]
    session.close()

    return {
        "date": str(date),
        "total_rooms": len(all_rooms),
        "booked_count": len(booked_list),
        "available_count": len(all_rooms) - len(booked_list),
        "bookings": booked_list,
        "available_rooms": available_rooms
    }


@app.get("/rooms-availability")
def rooms_availability(check_in: date_type, check_out: date_type):
    session = SessionLocal()
    all_rooms = session.query(Room).all()
    session.close()

    results = []
    for room in all_rooms:
        conflict = has_conflict(room.id, check_in, check_out)
        results.append({"room_id": room.id, "available": not conflict})

    return {
        "check_in": str(check_in),
        "check_out": str(check_out),
        "rooms": results
    }


@app.get("/search", response_class=HTMLResponse)
def search_page():
    html = """
    <html>
    <head>
        <title>Search & Book</title>
        <style>
            body {
                font-family: 'Segoe UI', Arial, sans-serif;
                background: #f4f6f8;
                margin: 0;
                padding: 30px;
                display: flex;
                justify-content: center;
            }
            .card {
                background: white;
                border-radius: 16px;
                box-shadow: 0 4px 20px rgba(0,0,0,0.08);
                padding: 28px;
                max-width: 480px;
                width: 100%;
            }
            h2 { text-align: center; margin-top: 0; }
            .date-row {
                display: flex;
                gap: 12px;
                margin-bottom: 16px;
            }
            .date-field { flex: 1; }
            label {
                display: block;
                font-size: 13px;
                color: #666;
                margin-bottom: 4px;
            }
            input {
                width: 100%;
                padding: 10px;
                border-radius: 8px;
                border: 1px solid #ccc;
                box-sizing: border-box;
                font-size: 14px;
            }
            button {
                width: 100%;
                padding: 12px;
                background: #222;
                color: white;
                border: none;
                border-radius: 8px;
                font-weight: bold;
                cursor: pointer;
                font-size: 15px;
                margin-top: 6px;
            }
            button:hover { background: #000; }
            .results { margin-top: 24px; }
            .room-card {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 12px 14px;
                border-radius: 10px;
                margin-bottom: 8px;
                font-size: 14px;
            }
            .room-available {
                background: #dcf5e0;
                cursor: pointer;
                transition: background 0.15s ease;
            }
            .room-available:hover { background: #b8ecc2; }
            .room-booked {
                background: #fbdada;
                color: #7a2c2c;
            }
            .badge {
                font-size: 12px;
                padding: 3px 10px;
                border-radius: 20px;
                background: white;
            }
            .book-form {
                margin-top: 18px;
                padding: 16px;
                border-radius: 10px;
                background: #f4f6f8;
                display: none;
            }
            .book-form input {
                margin-bottom: 10px;
            }
            .msg { margin-top: 10px; font-size: 14px; font-weight: bold; }
            .success-card {
                background: #dcf5e0;
                border-radius: 10px;
                padding: 16px;
                margin-top: 14px;
                display: none;
            }
            .success-card h3 { margin: 0 0 8px 0; color: #1a5c2a; }
            .back-link { text-align:center; margin-top:16px; }
            .back-link a { color:#222; font-size:13px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Check Availability & Book</h2>

            <div class="date-row">
                <div class="date-field">
                    <label>Check-in</label>
                    <input type="date" id="checkin">
                </div>
                <div class="date-field">
                    <label>Check-out</label>
                    <input type="date" id="checkout">
                </div>
            </div>
            <button onclick="searchRooms()">Check Availability</button>

            <div class="results" id="results"></div>

            <div class="book-form" id="bookForm">
                <div style="margin-bottom:10px; font-size:14px;">
                    Booking <b id="roomLabel"></b>
                </div>
                <input type="text" id="guestName" placeholder="Guest name">
                <input type="text" id="guestPhone" placeholder="Phone number">
                <button onclick="confirmBooking()">Confirm Booking</button>
                <div class="msg" id="bookMsg"></div>
            </div>

            <div class="success-card" id="successCard"></div>

            <div class="back-link"><a href="/calendar">&larr; Back to calendar</a></div>
        </div>

        <script>
            let selectedRoom = null;
            let bookingCheckin, bookingCheckout;

            async function searchRooms() {
                const checkin = document.getElementById('checkin').value;
                const checkout = document.getElementById('checkout').value;
                const results = document.getElementById('results');
                document.getElementById('bookForm').style.display = 'none';
                document.getElementById('successCard').style.display = 'none';

                if (!checkin || !checkout) {
                    results.innerHTML = "<div class='msg' style='color:red;'>Please select both dates.</div>";
                    return;
                }
                if (checkout <= checkin) {
                    results.innerHTML = "<div class='msg' style='color:red;'>Check-out must be after check-in.</div>";
                    return;
                }

                results.innerHTML = "Checking...";
                const res = await fetch('/rooms-availability?check_in=' + checkin + '&check_out=' + checkout);
                const data = await res.json();

                let html = "<div style='margin-bottom:10px; font-size:14px; color:#666;'>" +
                    checkin + " to " + checkout + "</div>";

                data.rooms.forEach(r => {
                    if (r.available) {
                        html += "<div class='room-card room-available' onclick='selectRoom(" + r.room_id + ", \\"" + checkin + "\\", \\"" + checkout + "\\")'>" +
                            "Room " + r.room_id + "<span class='badge'>Available — tap to book</span></div>";
                    } else {
                        html += "<div class='room-card room-booked'>Room " + r.room_id +
                            "<span class='badge'>Booked</span></div>";
                    }
                });

                results.innerHTML = html;
            }

            function selectRoom(roomId, checkin, checkout) {
                selectedRoom = roomId;
                bookingCheckin = checkin;
                bookingCheckout = checkout;
                document.getElementById('roomLabel').innerText =
                    "Room " + roomId + " (" + checkin + " to " + checkout + ")";
                document.getElementById('bookForm').style.display = 'block';
                document.getElementById('bookMsg').innerText = '';
            }

            async function confirmBooking() {
                const name = document.getElementById('guestName').value.trim();
                const phone = document.getElementById('guestPhone').value.trim();
                const msgBox = document.getElementById('bookMsg');

                if (!name || !phone) {
                    msgBox.style.color = 'red';
                    msgBox.innerText = 'Please enter guest name and phone.';
                    return;
                }

                const urlParams = new URLSearchParams(window.location.search);
                const fromWhatsapp = urlParams.get('from');

                const res = await fetch('/bookings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        room_id: selectedRoom,
                        guest_name: name,
                        guest_phone: phone,
                        check_in: bookingCheckin,
                        check_out: bookingCheckout,
                        from_whatsapp: fromWhatsapp
                    })
                });

                if (res.status === 200) {
                    const data = await res.json();
                    document.getElementById('bookForm').style.display = 'none';
                    const successCard = document.getElementById('successCard');
                    successCard.style.display = 'block';
                    successCard.innerHTML =
                        "<h3>Booking Created Successfully</h3>" +
                        "Booking ID: <b>BK" + String(data.booking_id).padStart(4, '0') + "</b><br>" +
                        "Room: " + selectedRoom + "<br>" +
                        "Dates: " + bookingCheckin + " to " + bookingCheckout;
                } else {
                    const err = await res.json();
                    msgBox.style.color = 'red';
                    msgBox.innerText = err.detail || 'Booking failed.';
                }
            }
        </script>
    </body>
    </html>
    """
    return html


@app.get("/calendar", response_class=HTMLResponse)
def booking_calendar(year: int = None, month: int = None):
    today = date_type.today()
    year = year or today.year
    month = month or today.month

    session = SessionLocal()
    bookings = session.query(Booking).filter(Booking.status == "confirmed").all()
    session.close()

    day_counts = {}
    cal = calendar.Calendar()
    for day in cal.itermonthdates(year, month):
        if day.month != month:
            continue
        count = sum(1 for b in bookings if b.check_in <= day < b.check_out)
        day_counts[day.day] = count

    month_name = calendar.month_name[month]
    cal_matrix = calendar.monthcalendar(year, month)

    prev_month = 12 if month == 1 else month - 1
    prev_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year

    rows = ""
    for week in cal_matrix:
        rows += "<tr>"
        for day in week:
            if day == 0:
                rows += "<td class='empty'></td>"
            else:
                count = day_counts.get(day, 0)
                if count == 0:
                    css_class = "free"
                elif count < 6:
                    css_class = "partial"
                else:
                    css_class = "full"
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
                rows += f"""
                <td class='{css_class}' onclick="showDetails('{date_str}')">
                    <div class='day-num'>{day}</div>
                    <div class='count'>{count} booked</div>
                </td>
                """
        rows += "</tr>"

    html = f"""
    <html>
    <head>
        <title>Booking Calendar</title>
        <style>
            body {{
                font-family: 'Segoe UI', Arial, sans-serif;
                background: #f4f6f8;
                margin: 0;
                padding: 30px;
                display: flex;
                gap: 30px;
                flex-wrap: wrap;
                justify-content: center;
            }}
            .calendar-container {{
                background: white;
                border-radius: 16px;
                box-shadow: 0 4px 20px rgba(0,0,0,0.08);
                padding: 24px;
                max-width: 700px;
                flex: 1;
            }}
            h2 {{ text-align: center; color: #222; margin-bottom: 4px; }}
            .nav {{ display: flex; justify-content: space-between; margin-bottom: 16px; }}
            .nav a {{
                text-decoration: none; background: #222; color: white;
                padding: 6px 14px; border-radius: 8px; font-size: 14px;
            }}
            .search-link {{ text-align: center; margin-bottom: 16px; }}
            .search-link a {{ color: #222; font-size: 13px; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th {{ padding: 8px; color: #666; font-size: 13px; }}
            td {{
                border-radius: 10px; text-align: center; padding: 10px 4px;
                cursor: pointer; transition: transform 0.15s ease; width: 14%;
            }}
            td:hover {{ transform: scale(1.05); }}
            td.empty {{ cursor: default; background: transparent; }}
            td.empty:hover {{ transform: none; }}
            .day-num {{ font-weight: bold; font-size: 16px; }}
            .count {{ font-size: 11px; margin-top: 4px; }}
            .free {{ background: #dcf5e0; }}
            .partial {{ background: #fdeecb; }}
            .full {{ background: #fbdada; }}

            .panel {{
                background: white; border-radius: 16px;
                box-shadow: 0 4px 20px rgba(0,0,0,0.08);
                padding: 24px; width: 320px; min-height: 200px;
            }}
            .panel h3 {{ margin-top: 0; }}
            .times {{
                font-size: 13px; color: #666; margin-bottom: 14px;
                background: #f4f6f8; padding: 8px 10px; border-radius: 8px;
            }}
            .room-row {{
                padding: 8px; border-radius: 8px; margin-bottom: 6px; font-size: 14px;
            }}
            .room-booked {{ background: #fbdada; }}
            .room-available {{
                background: #dcf5e0; cursor: pointer; transition: background 0.15s ease;
            }}
            .room-available:hover {{ background: #b8ecc2; }}
            .placeholder {{ color: #999; font-size: 14px; }}

            .book-form {{
                margin-top: 14px; padding: 14px; border-radius: 10px;
                background: #f4f6f8; display: none;
            }}
            .book-form input {{
                width: 100%; padding: 8px; margin-bottom: 8px;
                border-radius: 6px; border: 1px solid #ccc; box-sizing: border-box;
            }}
            .book-form button {{
                width: 100%; padding: 10px; background: #222; color: white;
                border: none; border-radius: 6px; cursor: pointer; font-weight: bold;
            }}
            .book-msg {{ margin-top: 8px; font-size: 13px; }}
        </style>
    </head>
    <body>
        <div class="calendar-container">
            <div class="nav">
                <a href="/calendar?year={prev_year}&month={prev_month}">&larr; Prev</a>
                <h2>{month_name} {year}</h2>
                <a href="/calendar?year={next_year}&month={next_month}">Next &rarr;</a>
            </div>
            <div class="search-link"><a href="/search">&rarr; Search & Book by dates</a></div>
            <table>
                <tr><th>Mon</th><th>Tue</th><th>Wed</th><th>Thu</th><th>Fri</th><th>Sat</th><th>Sun</th></tr>
                {rows}
            </table>
        </div>

        <div class="panel" id="panel">
            <h3>Select a date</h3>
            <div class="placeholder">Click any day to see room details.</div>
        </div>

        <script>
            let selectedDate = null;
            let selectedRoom = null;

            async function showDetails(dateStr) {{
                selectedDate = dateStr;
                const panel = document.getElementById('panel');
                panel.innerHTML = "<h3>Loading...</h3>";
                const res = await fetch('/day-details?date=' + dateStr);
                const data = await res.json();

                let html = "<h3>" + dateStr + "</h3>";
                html += "<div class='times'>Check-in: 12:00 PM &nbsp;|&nbsp; Check-out: 11:00 AM</div>";
                html += "<p><b>" + data.booked_count + "</b> booked / <b>" + data.total_rooms + "</b> total rooms</p>";

                if (data.bookings.length > 0) {{
                    html += "<div><b>Booked rooms:</b></div>";
                    data.bookings.forEach(b => {{
                        html += "<div class='room-row room-booked'>Room " + b.room_id + " — " + b.guest_name + " (" + b.guest_phone + ") — <b>Booking ID: BK" + String(b.booking_id).padStart(4, '0') + "</b></div>";
                    }});
                }}

                if (data.available_rooms.length > 0) {{
                    html += "<div style='margin-top:10px;'><b>Available rooms (click to book):</b></div>";
                    data.available_rooms.forEach(r => {{
                        html += "<div class='room-row room-available' onclick='openBookForm(" + r + ")'>Room " + r + "</div>";
                    }});
                }}

                html += `
                    <div class="book-form" id="bookForm">
                        <div style="margin-bottom:8px; font-size:13px;">Booking <b id="bookRoomLabel"></b></div>
                        <input type="date" id="checkoutInput" />
                        <input type="text" id="nameInput" placeholder="Guest name" />
                        <input type="text" id="phoneInput" placeholder="Phone number" />
                        <button onclick="submitBooking()">Confirm Booking</button>
                        <div class="book-msg" id="bookMsg"></div>
                    </div>
                `;

                panel.innerHTML = html;
            }}

            function openBookForm(roomId) {{
                selectedRoom = roomId;
                document.getElementById('bookRoomLabel').innerText = "Room " + roomId + " — check-in " + selectedDate;
                document.getElementById('bookForm').style.display = 'block';
                document.getElementById('bookMsg').innerText = '';
            }}

            async function submitBooking() {{
                const checkout = document.getElementById('checkoutInput').value;
                const name = document.getElementById('nameInput').value.trim();
                const phone = document.getElementById('phoneInput').value.trim();
                const msgBox = document.getElementById('bookMsg');

                if (!checkout || !name || !phone) {{
                    msgBox.style.color = 'red';
                    msgBox.innerText = 'Please fill in check-out date, name, and phone.';
                    return;
                }}

                const res = await fetch('/bookings', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        room_id: selectedRoom,
                        guest_name: name,
                        guest_phone: phone,
                        check_in: selectedDate,
                        check_out: checkout
                    }})
                }});

                if (res.status === 200) {{
                    msgBox.style.color = 'green';
                    msgBox.innerText = 'Booking confirmed!';
                    setTimeout(() => showDetails(selectedDate), 1000);
                }} else {{
                    const err = await res.json();
                    msgBox.style.color = 'red';
                    msgBox.innerText = err.detail || 'Booking failed.';
                }}
            }}
        </script>
    </body>
    </html>
    """
    return html


@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...), From: str = Form(...)):
    msg = Body.strip()
    session = SESSIONS.get(From, {"step": None, "data": {}})
    step = session.get("step")
    data = session.get("data", {})
    reply = ""

    try:
        if step is None:
            if msg == "1":
                reply = f"Check availability and book here:\n{APP_BASE_URL}/search?from={From}"
                session = {"step": None, "data": {}}
            elif msg == "2":
                session = {"step": "book_room", "data": {}}
                reply = "Which room number would you like to book?"
            elif msg == "3":
                session = {"step": "cancel_id", "data": {}}
                reply = "Enter the booking ID to cancel."
            elif msg == "4":
                s = SessionLocal()
                today = date_type.today()
                count = s.query(Booking).filter(
                    Booking.check_in <= today,
                    Booking.check_out > today,
                    Booking.status == "confirmed"
                ).count()
                s.close()
                reply = f"Total active bookings today: {count}"
            else:
                reply = "Choose an option:\n1. Check Availability & Book (Calendar)\n2. Book (manual entry)\n3. Cancel\n4. Total Bookings"

        elif step == "book_room":
            data["room_id"] = int(msg)
            session = {"step": "book_checkin", "data": data}
            reply = "Check-in date? (YYYY-MM-DD)"

        elif step == "book_checkin":
            data["check_in"] = date_type.fromisoformat(msg)
            session = {"step": "book_checkout", "data": data}
            reply = "Check-out date? (YYYY-MM-DD)"

        elif step == "book_checkout":
            check_out = date_type.fromisoformat(msg)
            if check_out <= data["check_in"]:
                reply = "Check-out date must be after check-in date. Check-out date? (YYYY-MM-DD)"
                session = {"step": "book_checkout", "data": data}
            else:
                data["check_out"] = check_out
                session = {"step": "book_details", "data": data}
                reply = "Send name and phone number, separated by a comma.\nExample: Ravi, 9876543210"

        elif step == "book_details":
            name_part, phone_part = msg.split(",")
            guest_name = name_part.strip()
            guest_phone = phone_part.strip()
            room_id = data.get("room_id")
            check_in = data.get("check_in")
            check_out = data.get("check_out")

            if has_conflict(room_id, check_in, check_out):
                reply = f"Room {room_id} was just booked by someone else. Booking rejected."
            else:
                s = SessionLocal()
                new_booking = Booking(
                    room_id=room_id, guest_name=guest_name, guest_phone=guest_phone,
                    check_in=check_in, check_out=check_out, status="confirmed"
                )
                s.add(new_booking)
                s.commit()
                s.refresh(new_booking)
                s.close()
                reply = f"Booking confirmed. Booking ID: BK{new_booking.id:04d}"
            session = {"step": None, "data": {}}

        elif step == "cancel_id":
            raw = msg.strip().upper().replace("BK", "")
            try:
                booking_id = int(raw)
            except ValueError:
                reply = "Invalid booking ID. Please enter a numeric booking ID (e.g. 12 or BK0012)."
                session = {"step": None, "data": {}}
                SESSIONS[From] = session
                twiml = f"<Response><Message>{reply}</Message></Response>"
                return PlainTextResponse(content=twiml, media_type="application/xml")

            s = SessionLocal()
            booking = s.query(Booking).filter(Booking.id == booking_id).first()
            if not booking:
                reply = "Booking not found."
            elif booking.status == "cancelled":
                reply = "Already cancelled."
            else:
                booking.status = "cancelled"
                s.commit()
                reply = f"Booking BK{booking_id:04d} cancelled."
            s.close()
            session = {"step": None, "data": {}}

        else:
            session = {"step": None, "data": {}}
            reply = "Choose an option:\n1. Check Availability & Book (Calendar)\n2. Book (manual entry)\n3. Cancel\n4. Total Bookings"

        SESSIONS[From] = session

    except Exception as e:
        print("WHATSAPP WEBHOOK ERROR:", repr(e))
        reply = "Sorry, something went wrong. Type anything to see the menu again."
        SESSIONS[From] = {"step": None, "data": {}}

    twiml = f"<Response><Message>{reply}</Message></Response>"
    return PlainTextResponse(content=twiml, media_type="application/xml")