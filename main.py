from fastapi import FastAPI, HTTPException, Form
from fastapi.responses import PlainTextResponse, HTMLResponse
from pydantic import BaseModel
from datetime import date as date_type, timedelta
import calendar
import json
import os
from xml.sax.saxutils import escape as xml_escape
from dotenv import load_dotenv
from twilio.rest import Client
from models import SessionLocal, Booking, Room
from conflict_check import has_conflict

load_dotenv()

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")
CONTENT_SID_DATE_PICKER = os.environ.get("CONTENT_SID_DATE_PICKER")

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = FastAPI()

SESSIONS = {}


def get_date_options(days=10):
    """Return the next `days` days with a title/description and how many rooms are free that day."""
    session = SessionLocal()
    all_rooms = session.query(Room).all()
    total_rooms = len(all_rooms)
    options = []
    for i in range(days):
        d = date_type.today() + timedelta(days=i)
        bookings_that_day = session.query(Booking).filter(
            Booking.status == "confirmed",
            Booking.check_in <= d,
            Booking.check_out > d
        ).count()
        free = total_rooms - bookings_that_day
        title = d.strftime("%b %d")
        desc = "Fully booked" if free <= 0 else f"{free} room{'s' if free != 1 else ''} free"
        options.append({"date": d.isoformat(), "title": title, "description": desc})
    session.close()
    return options


def send_date_picker(to_whatsapp_number, body_text="Select a check-in date:"):
    """Sends the List Picker content template with the next 10 days as options.
    Returns the list of options sent, so the caller can store a title->date mapping."""
    options = get_date_options(10)

    content_variables = {}
    for idx, opt in enumerate(options):
        content_variables[str(idx * 2 + 1)] = opt["title"]
        content_variables[str(idx * 2 + 2)] = opt["description"]

    twilio_client.messages.create(
        content_sid=CONTENT_SID_DATE_PICKER,
        content_variables=json.dumps(content_variables),
        from_=TWILIO_WHATSAPP_FROM,
        to=to_whatsapp_number
    )
    return options

class BookingRequest(BaseModel):
    room_id: int
    guest_name: str
    guest_phone: str
    check_in: date_type
    check_out: date_type


class ModifyBookingRequest(BaseModel):
    check_in: date_type
    check_out: date_type


@app.get("/")
def home():
    return {"message": "Resort booking bot is running"}


def format_whatsapp_number(phone: str) -> str:
    """Normalize a plain phone number into Twilio's whatsapp:+<countrycode><number> format.
    Assumes India (+91) if no country code is given."""
    cleaned = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if cleaned.startswith("+"):
        return f"whatsapp:{cleaned}"
    if cleaned.startswith("91") and len(cleaned) > 10:
        return f"whatsapp:+{cleaned}"
    return f"whatsapp:+91{cleaned}"


def send_booking_confirmation(guest_phone: str, booking_id: int, room_id: int, check_in, check_out):
    """Sends a WhatsApp confirmation message to the guest after a booking is created.
    Fails silently (logs only) so a messaging failure never blocks the booking itself."""
    try:
        to_number = format_whatsapp_number(guest_phone)
        body = (
            f"Booking confirmed!\n"
            f"Booking ID: BK{booking_id:04d}\n"
            f"Room: {room_id}\n"
            f"Dates: {check_in} to {check_out}"
        )
        twilio_client.messages.create(
            body=body,
            from_=TWILIO_WHATSAPP_FROM,
            to=to_number
        )
    except Exception as e:
        print("BOOKING CONFIRMATION SEND ERROR:", repr(e))


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

    send_booking_confirmation(
        booking.guest_phone, new_booking.id, booking.room_id, booking.check_in, booking.check_out
    )

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
        "available_rooms": available_rooms,
        "is_past": date < date_type.today()
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


@app.get("/check-availability", response_class=HTMLResponse)
def availability_page():
    html = """
    <html>
    <head>
        <title>Check Availability</title>
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
            .room-free { background: #dcf5e0; }
            .room-booked { background: #fbdada; color: #7a2c2c; }
            .badge {
                font-size: 12px;
                padding: 3px 10px;
                border-radius: 20px;
                background: white;
            }
            .msg { margin-top: 10px; font-size: 14px; font-weight: bold; }
            .back-link { text-align:center; margin-top:16px; }
            .back-link a { color:#222; font-size:13px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Check Availability</h2>

            <div class="date-row">
                <div class="date-field">
                    <label>Check-in</label>
                    <input type="date" id="checkin" min="">
                </div>
                <div class="date-field">
                    <label>Check-out</label>
                    <input type="date" id="checkout" min="">
                </div>
            </div>
            <button onclick="searchRooms()">Check Availability</button>

            <div class="results" id="results"></div>

            <div class="back-link"><a href="/book">&rarr; Go to Book a Room</a></div>
        </div>

        <script>
            const todayStr = new Date().toISOString().split('T')[0];
            document.getElementById('checkin').setAttribute('min', todayStr);
            document.getElementById('checkout').setAttribute('min', todayStr);

            async function searchRooms() {
                const checkin = document.getElementById('checkin').value;
                const checkout = document.getElementById('checkout').value;
                const results = document.getElementById('results');

                if (!checkin || !checkout) {
                    results.innerHTML = "<div class='msg' style='color:red;'>Please select both dates.</div>";
                    return;
                }
                if (checkin < todayStr) {
                    results.innerHTML = "<div class='msg' style='color:#7a2c2c;'>This date has already passed. Please choose a future date.</div>";
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
                        html += "<div class='room-card room-free'>Room " + r.room_id +
                            "<span class='badge'>Available</span></div>";
                    } else {
                        html += "<div class='room-card room-booked'>Room " + r.room_id +
                            "<span class='badge'>Booked</span></div>";
                    }
                });

                results.innerHTML = html;
            }
        </script>
    </body>
    </html>
    """
    return html


@app.get("/book", response_class=HTMLResponse)
def book_page():
    html = """
    <html>
    <head>
        <title>Book a Room</title>
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
            input, select {
                width: 100%;
                padding: 10px;
                border-radius: 8px;
                border: 1px solid #ccc;
                box-sizing: border-box;
                font-size: 14px;
                margin-bottom: 12px;
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
            <h2>Book a Room</h2>

            <label>Room</label>
            <select id="roomId">
                <option value="1">Room 1</option>
                <option value="2">Room 2</option>
                <option value="3">Room 3</option>
                <option value="4">Room 4</option>
                <option value="5">Room 5</option>
                <option value="6">Room 6</option>
            </select>

            <div class="date-row">
                <div class="date-field">
                    <label>Check-in</label>
                    <input type="date" id="checkin" min="">
                </div>
                <div class="date-field">
                    <label>Check-out</label>
                    <input type="date" id="checkout" min="">
                </div>
            </div>

            <label>Guest name</label>
            <input type="text" id="guestName" placeholder="Guest name">

            <label>Phone number</label>
            <input type="text" id="guestPhone" placeholder="Phone number">

            <button onclick="confirmBooking()">Confirm Booking</button>
            <div class="msg" id="bookMsg"></div>

            <div class="success-card" id="successCard"></div>

            <div class="back-link"><a href="/check-availability">&larr; Check Availability first</a></div>
        </div>

        <script>
            const todayStr = new Date().toISOString().split('T')[0];
            document.getElementById('checkin').setAttribute('min', todayStr);
            document.getElementById('checkout').setAttribute('min', todayStr);

            async function confirmBooking() {
                const roomId = parseInt(document.getElementById('roomId').value);
                const checkin = document.getElementById('checkin').value;
                const checkout = document.getElementById('checkout').value;
                const name = document.getElementById('guestName').value.trim();
                const phone = document.getElementById('guestPhone').value.trim();
                const msgBox = document.getElementById('bookMsg');
                msgBox.innerText = '';

                if (!checkin || !checkout) {
                    msgBox.style.color = 'red';
                    msgBox.innerText = 'Please select check-in and check-out dates.';
                    return;
                }
                if (checkin < todayStr) {
                    msgBox.style.color = '#7a2c2c';
                    msgBox.innerText = 'This date has already passed. Please choose a future date.';
                    return;
                }
                if (checkout <= checkin) {
                    msgBox.style.color = 'red';
                    msgBox.innerText = 'Check-out must be after check-in.';
                    return;
                }
                if (!name || !phone) {
                    msgBox.style.color = 'red';
                    msgBox.innerText = 'Please enter guest name and phone.';
                    return;
                }

                const res = await fetch('/bookings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        room_id: roomId,
                        guest_name: name,
                        guest_phone: phone,
                        check_in: checkin,
                        check_out: checkout
                    })
                });

                if (res.status === 200) {
                    const data = await res.json();
                    const successCard = document.getElementById('successCard');
                    successCard.style.display = 'block';
                    successCard.innerHTML =
                        "<h3>Booking Created Successfully</h3>" +
                        "Booking ID: <b>BK" + String(data.booking_id).padStart(4, '0') + "</b><br>" +
                        "Room: " + roomId + "<br>" +
                        "Dates: " + checkin + " to " + checkout;
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
            <div class="search-link"><a href="/check-availability">&rarr; Check Availability</a> &nbsp;|&nbsp; <a href="/book">Book a Room</a></div>
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
                    if (data.is_past) {{
                        html += "<div style='margin-top:10px;'><b>Rooms not booked that day:</b></div>";
                        data.available_rooms.forEach(r => {{
                            html += "<div class='room-row' style='background:#eee; color:#888;'>Room " + r + "</div>";
                        }});
                        html += "<div style='margin-top:8px; font-size:12px; color:#999;'>This date has already passed — booking is disabled.</div>";
                    }} else {{
                        html += "<div style='margin-top:10px;'><b>Available rooms (click to book):</b></div>";
                        data.available_rooms.forEach(r => {{
                            html += "<div class='room-row room-available' onclick='openBookForm(" + r + ")'>Room " + r + "</div>";
                        }});
                    }}
                }}

                if (!data.is_past) {{
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
                }}

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
                reply = "Check availability and book a room here:\nhttps://fiction-attribute-limping.ngrok-free.dev/calendar"
                session = {"step": None, "data": {}}
            elif msg == "2":
                session = {"step": "cancel_id", "data": {}}
                reply = "Enter the booking ID to cancel."
            elif msg == "3":
                s = SessionLocal()
                last_booking = s.query(Booking).filter(
                    Booking.status == "confirmed"
                ).order_by(Booking.id.desc()).first()
                s.close()
                if last_booking:
                    reply = (
                        f"Last booking:\n"
                        f"Booking ID: BK{last_booking.id:04d}\n"
                        f"Room: {last_booking.room_id}\n"
                        f"Guest: {last_booking.guest_name} ({last_booking.guest_phone})\n"
                        f"Dates: {last_booking.check_in} to {last_booking.check_out}"
                    )
                else:
                    reply = "No bookings found yet."
                session = {"step": None, "data": {}}
            else:
                reply = "Choose an option:\n1. Check Availability and Book\n2. Cancel\n3. My Last Booking"

        elif step == "avail_room":
            data["room_id"] = int(msg)
            options = send_date_picker(From, "Select a check-in date:")
            data["date_options"] = {opt["title"]: opt["date"] for opt in options}
            session = {"step": "avail_pick_checkin", "data": data}
            reply = ""

        elif step == "avail_pick_checkin":
            date_iso = data.get("date_options", {}).get(msg.strip())
            if not date_iso:
                reply = "Please tap one of the dates from the list above."
                session = {"step": "avail_pick_checkin", "data": data}
            else:
                data["check_in"] = date_type.fromisoformat(date_iso)
                data.pop("date_options", None)
                session = {"step": "avail_checkout", "data": data}
                reply = "Check-out date? (YYYY-MM-DD)"

        elif step == "avail_checkout":
            check_out = date_type.fromisoformat(msg)
            if check_out <= data["check_in"]:
                reply = "Check-out date must be after check-in date. Check-out date? (YYYY-MM-DD)"
                session = {"step": "avail_checkout", "data": data}
            else:
                data["check_out"] = check_out
                conflict = has_conflict(data["room_id"], data["check_in"], data["check_out"])
                available = not conflict
                reply = f"Room {data['room_id']} is {'available' if available else 'NOT available'} for {data['check_in']} to {data['check_out']}."
                if available:
                    reply += "\n\nType 2 to book this exact room and dates."
                    session = {
                        "step": None,
                        "data": {
                            "last_room": data["room_id"],
                            "last_checkin": data["check_in"],
                            "last_checkout": data["check_out"],
                            "last_available": True
                        }
                    }
                else:
                    session = {"step": None, "data": {}}

        elif step == "book_room":
            data["room_id"] = int(msg)
            options = send_date_picker(From, "Select a check-in date:")
            data["date_options"] = {opt["title"]: opt["date"] for opt in options}
            session = {"step": "book_pick_checkin", "data": data}
            reply = ""

        elif step == "book_pick_checkin":
            date_iso = data.get("date_options", {}).get(msg.strip())
            if not date_iso:
                reply = "Please tap one of the dates from the list above."
                session = {"step": "book_pick_checkin", "data": data}
            else:
                data["check_in"] = date_type.fromisoformat(date_iso)
                data.pop("date_options", None)
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
            room_id = data.get("room_id") or data.get("last_room")
            check_in = data.get("check_in") or data.get("last_checkin")
            check_out = data.get("check_out") or data.get("last_checkout")

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
                twiml = f"<Response><Message>{xml_escape(reply)}</Message></Response>"
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
            reply = "Choose an option:\n1. Check Availability and Book\n2. Cancel\n3. My Last Booking"

        SESSIONS[From] = session

    except Exception as e:
        print("WHATSAPP WEBHOOK ERROR:", repr(e))
        reply = "Sorry, something went wrong. Type anything to see the menu again."
        SESSIONS[From] = {"step": None, "data": {}}

    if reply:
        twiml = f"<Response><Message>{xml_escape(reply)}</Message></Response>"
    else:
        twiml = "<Response></Response>"
    return PlainTextResponse(content=twiml, media_type="application/xml")